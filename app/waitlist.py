"""限量活动候补席位的一次性试算逻辑。

规则：
- 候补顺序固定：**优先级高者优先；同优先级报名时刻早者优先；再同则编号
  Unicode 字典序**。起始时刻按该顺序邀请至满额（席位数）。
- 邀请在分钟 ``T`` 发出、保留 ``ttl`` 分钟：**截止时刻为 ``T + ttl``，
  截止当分钟仍可被本人确认；下一分钟（``T + ttl + 1``）自动失效**。
  自动失效在该分钟任何事件之前处理：失效者退出候补，空位立即（同一分钟）
  按顺序补邀下一人。
- 事件（确认 / 取消）按时刻非递减排列，同刻按输入顺序逐个处理：
  - **确认**：仅当本人持有仍有效的邀请（已发出且尚未到自动失效分钟）时
    生效，转为已确认；否则（编号未知、尚在待邀、已确认过、已退出）
    整批拒绝。
  - **取消**：待邀者直接退出（不释放席位）；持邀或已确认者退出并释放
    席位，空位立即补位；编号未知或已退出（已取消 / 已失效）者整批拒绝。
- 已发出的邀请不撤换；确认不释放席位；结算时刻之后不再处理任何失效，
  届时仍待决的邀请结果为 ``pending``。

任何业务冲突都抛出 :class:`WaitlistRejected`，本模块不“提交”任何中间
结果，调用方据此整批拒绝、绝不返回部分试算。所有时刻均为整分钟的
UTC ``datetime``。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

__all__ = [
    "CandidateInput",
    "TrialEvent",
    "Invitation",
    "CandidateOutcome",
    "WaitlistTrialResult",
    "WaitlistRejected",
    "simulate",
]

# 候补者最终状态
WAITING = "waiting"  # 待邀：已报名、尚未被邀请
INVITED = "invited"  # 持邀：邀请仍待决
CONFIRMED = "confirmed"  # 已确认
CANCELLED = "cancelled"  # 已取消（取消事件导致退出）
EXPIRED = "expired"  # 已失效（邀请自动失效导致退出）

# 邀请结果
RESULT_PENDING = "pending"  # 结算时仍待决
RESULT_CONFIRMED = "confirmed"  # 本人已在截止当分钟（含）前确认
RESULT_EXPIRED = "expired"  # 自动失效
RESULT_CANCELLED = "cancelled"  # 持邀期间被取消

_EXITED = (CANCELLED, EXPIRED)


@dataclass(frozen=True)
class CandidateInput:
    candidate_id: str
    priority: int
    registered_at: datetime  # 报名时刻（整分钟 UTC，不晚于起始时刻）


@dataclass(frozen=True)
class TrialEvent:
    """一个确认 / 取消事件；``index`` 为输入顺序号（0 起），用于同刻排序与错误定位。"""

    kind: Literal["confirm", "cancel"]
    candidate_id: str
    at: datetime
    index: int


@dataclass
class Invitation:
    """一次邀请：发出时刻、截止时刻（含当分钟可确认）与结果。"""

    candidate_id: str
    seq: int  # 发出顺序号（0 起）
    issued_at: datetime
    deadline: datetime  # 截止时刻 = 发出 + ttl；该分钟仍可确认
    expiry_at: datetime  # 自动失效分钟 = 截止 + 1
    result: str = RESULT_PENDING
    resolved_at: datetime | None = None  # 结果确定时刻；pending 时为 None


@dataclass(frozen=True)
class CandidateOutcome:
    candidate_id: str
    status: str  # waiting / invited / confirmed / cancelled / expired


@dataclass(frozen=True)
class WaitlistTrialResult:
    invitations: list[Invitation]  # 按发出顺序
    candidates: list[CandidateOutcome]  # 按候选人输入顺序


class WaitlistRejected(Exception):
    """整批试算必须被拒绝的业务冲突；不允许返回任何部分结果。

    ``error`` 取值：
    - ``cancel_target_invalid``：取消对象未知或已退出
      （reason: ``not_found`` / ``already_exited``）
    - ``no_valid_invitation``：确认时本人无有效邀请
      （reason: ``not_found`` / ``not_invited`` / ``already_confirmed`` /
      ``already_exited``）
    """

    def __init__(
        self,
        error: str,
        candidate_id: str,
        message: str,
        *,
        event_index: int,
        at: datetime,
        reason: str,
    ) -> None:
        self.error = error
        self.candidate_id = candidate_id
        self.message = message
        self.event_index = event_index
        self.at = at
        self.reason = reason
        super().__init__(f"{error}: {message}")

    def detail(self) -> dict:
        """供 HTTP 层使用的结构化错误信息。"""
        return {
            "error": self.error,
            "candidate_id": self.candidate_id,
            "event_index": self.event_index,
            "at_utc": self.at.isoformat().replace("+00:00", "Z"),
            "reason": self.reason,
        }


def simulate(
    *,
    seats: int,
    start_at: datetime,
    ttl_minutes: int,
    settle_at: datetime,
    candidates: list[CandidateInput],
    events: list[TrialEvent],
) -> WaitlistTrialResult:
    """按规则试算候补席位，返回全部邀请与每人的最终状态。

    任一业务冲突抛 :class:`WaitlistRejected`，不返回任何部分结果。
    调用方须保证：``seats >= 1``、``ttl_minutes >= 1``、候选人编号唯一、
    报名时刻不晚于 ``start_at``、事件按时刻非递减且落在
    ``[start_at, settle_at]`` 内、``settle_at >= start_at``。
    """
    # 候补顺序：优先级降序、报名时刻升序、编号 Unicode 字典序（固定不变）
    queue_order = sorted(
        candidates,
        key=lambda c: (-c.priority, c.registered_at, c.candidate_id),
    )
    status = {c.candidate_id: WAITING for c in candidates}
    waiting: deque[str] = deque(c.candidate_id for c in queue_order)
    seats_free = seats
    invitations: list[Invitation] = []
    active: dict[str, Invitation] = {}  # 持邀（未确认）候选 -> 其邀请
    # 待失效邀请按发出顺序排列；ttl 固定、发出时刻非递减，
    # 故自动失效分钟也非递减，可用 FIFO 队列按序取出
    pending_expiry: deque[Invitation] = deque()
    ttl = timedelta(minutes=ttl_minutes)
    one_minute = timedelta(minutes=1)

    def issue_invites(at: datetime) -> None:
        """把当前全部空位按候补顺序立即补邀（同一分钟）。"""
        nonlocal seats_free
        while seats_free > 0 and waiting:
            cid = waiting[0]
            if status[cid] != WAITING:
                waiting.popleft()  # 等待期间已被取消，跳过
                continue
            waiting.popleft()
            inv = Invitation(
                candidate_id=cid,
                seq=len(invitations),
                issued_at=at,
                deadline=at + ttl,
                expiry_at=at + ttl + one_minute,
            )
            invitations.append(inv)
            active[cid] = inv
            status[cid] = INVITED
            seats_free -= 1
            pending_expiry.append(inv)

    def next_pending_expiry() -> Invitation | None:
        # 队首已确认 / 已取消的邀请直接丢弃
        while pending_expiry and pending_expiry[0].result != RESULT_PENDING:
            pending_expiry.popleft()
        return pending_expiry[0] if pending_expiry else None

    # 起始时刻：按候补顺序邀请至满额
    issue_invites(start_at)

    ev_idx = 0
    n_events = len(events)
    while True:
        head = next_pending_expiry()
        expiry_at = head.expiry_at if head is not None else None
        next_ev_at = events[ev_idx].at if ev_idx < n_events else None

        if (
            expiry_at is not None
            and expiry_at <= settle_at
            and (next_ev_at is None or expiry_at <= next_ev_at)
        ):
            # 自动失效：同一分钟的一起处理，且先于该分钟的事件
            t = expiry_at
            while True:
                inv = next_pending_expiry()
                if inv is None or inv.expiry_at != t:
                    break
                pending_expiry.popleft()
                cid = inv.candidate_id
                inv.result = RESULT_EXPIRED
                inv.resolved_at = t
                status[cid] = EXPIRED
                del active[cid]
                seats_free += 1
            # 失效者退出后空位立即补邀下一人（同一分钟）
            issue_invites(t)
            continue

        if next_ev_at is None:
            break

        # 同一分钟的事件按输入顺序逐个处理
        t = next_ev_at
        while ev_idx < n_events and events[ev_idx].at == t:
            ev = events[ev_idx]
            ev_idx += 1
            cid = ev.candidate_id
            if ev.kind == "confirm":
                inv = active.get(cid)
                if inv is None:
                    raise WaitlistRejected(
                        "no_valid_invitation",
                        cid,
                        f"confirm of {cid!r} at event #{ev.index} has no "
                        "valid invitation; the whole batch is rejected",
                        event_index=ev.index,
                        at=ev.at,
                        reason=_confirm_reason(status.get(cid)),
                    )
                inv.result = RESULT_CONFIRMED
                inv.resolved_at = t
                status[cid] = CONFIRMED
                del active[cid]
            else:  # cancel
                st = status.get(cid)
                if st is None or st in _EXITED:
                    reason = "not_found" if st is None else "already_exited"
                    raise WaitlistRejected(
                        "cancel_target_invalid",
                        cid,
                        f"cancel of {cid!r} at event #{ev.index} targets a "
                        f"candidate that is {reason.replace('_', ' ')}; "
                        "the whole batch is rejected",
                        event_index=ev.index,
                        at=ev.at,
                        reason=reason,
                    )
                if st == INVITED:
                    inv = active.pop(cid)
                    inv.result = RESULT_CANCELLED
                    inv.resolved_at = t
                    seats_free += 1
                elif st == CONFIRMED:
                    seats_free += 1
                # 待邀者退出不释放席位
                status[cid] = CANCELLED
                # 释放的席位立即补位（同一分钟、先于同刻后续事件）；
                # 待邀者取消时不变席位，此处为空操作
                issue_invites(t)

    # 结算：之后不再处理失效；仍待决的邀请保持 pending，
    # 待邀 / 持邀者保持其状态
    outcomes = [
        CandidateOutcome(candidate_id=c.candidate_id, status=status[c.candidate_id])
        for c in candidates
    ]
    return WaitlistTrialResult(invitations=invitations, candidates=outcomes)


def _confirm_reason(st: str | None) -> str:
    """确认失败的原因（按候选人当前状态区分）。"""
    if st is None:
        return "not_found"
    if st == WAITING:
        return "not_invited"
    if st == CONFIRMED:
        return "already_confirmed"
    return "already_exited"
