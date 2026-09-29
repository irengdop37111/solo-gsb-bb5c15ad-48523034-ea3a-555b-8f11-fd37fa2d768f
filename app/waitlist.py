"""限量活动候补席位试算逻辑（一次请求原子试算，冲突整批拒绝）。

规则：
- 起始时刻按 **优先级高（priority 数值大者）、报名早（registered_minute
  小者）、编号 Unicode 字典序** 把候补者排序，自队首邀请至满额。
- 每次邀请在 ``sent_minute`` 发出，保留 ``hold_minutes`` 分钟，
  ``deadline_minute = sent_minute + hold_minutes``；**截止当分钟仍可由本人
  确认，截止的下一分钟（deadline_minute + 1）自动失效**。失效者退出候补，
  其席位立即邀请当时队首的下一人。
- 时间按整分钟推进：每分钟先处理该分钟的全部自动失效，再按输入顺序处理
  该分钟的确认 / 取消事件；同刻事件严格按输入顺序。
- 取消**待邀者**：仅退出候补队列，不释放席位；取消**持邀或已确认者**：
  邀请 / 席位立即释放并马上补位给下一人。已发出的邀请不撤换。
- 确认仅在本人当时持有**有效（已发出、未确认、未取消、未失效）**邀请时
  合法；确认后邀请即了结（之后再取消属于取消已确认者，邀请结果仍为
  ``confirmed``）。
- 取消未知编号或已退出（已失效 / 已取消）对象、确认时本人无有效邀请，
  均为业务冲突，抛 :class:`TrialRejected`，整批拒绝、不返回任何部分试算。

输入层面的形状 / 取值校验（编号重复、非正席位或保留时长、报名晚于起始、
事件越界或倒序等）由 Pydantic 模型负责（HTTP 422）；本模块只在事件流
处理中发现动态业务冲突（HTTP 409）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import heapq
from itertools import count
from typing import Literal

__all__ = [
    "CandidateInput",
    "Event",
    "Invitation",
    "PersonResult",
    "TrialResult",
    "TrialRejected",
    "simulate",
    "CONFIRM",
    "CANCEL",
    "WAITING",
    "INVITED",
    "CONFIRMED",
    "EXPIRED",
    "CANCELED",
    "PENDING",
    "CONFIRMED_RESULT",
    "EXPIRED_RESULT",
    "CANCELED_RESULT",
]

# 事件类型
CONFIRM: Literal["confirm"] = "confirm"
CANCEL: Literal["cancel"] = "cancel"

# 人员最终 / 过程状态
WAITING: Literal["waiting"] = "waiting"  # 仍在候补队列，尚未获邀
INVITED: Literal["invited"] = "invited"  # 持有效邀请（结算时仍未了结）
CONFIRMED: Literal["confirmed"] = "confirmed"  # 已确认占座（结算时仍持有）
EXPIRED: Literal["expired"] = "expired"  # 邀请自动失效，已退出
CANCELED: Literal["canceled"] = "canceled"  # 本人取消，已退出

# 邀请结果
PENDING: Literal["pending"] = "pending"  # 结算时仍有效
CONFIRMED_RESULT: Literal["confirmed"] = "confirmed"  # 本人已确认
EXPIRED_RESULT: Literal["expired"] = "expired"  # 自动失效
CANCELED_RESULT: Literal["canceled"] = "canceled"  # 持邀期间本人取消

_EXITED = frozenset({EXPIRED, CANCELED})


@dataclass(frozen=True)
class CandidateInput:
    """一名候补者：编号、优先级（数值大者优先）、报名时刻（整分钟 UTC）。"""

    id: str
    priority: int
    registered_minute: int


@dataclass(frozen=True)
class Event:
    """一个确认 / 取消事件：``at_minute`` 非递减，落在 [start, settle]。"""

    at_minute: int
    kind: Literal["confirm", "cancel"]
    candidate_id: str


@dataclass(frozen=True)
class Invitation:
    """一次邀请：发出 / 截止时刻与最终结果。

    ``result_minute`` 为结果落定时刻（确认 / 失效 / 取消所在分钟）；
    结算时仍有效（``pending``）时为 ``None``。
    """

    candidate_id: str
    sent_minute: int
    deadline_minute: int
    result: Literal["pending", "confirmed", "expired", "canceled"] = PENDING
    result_minute: int | None = None


@dataclass(frozen=True)
class PersonResult:
    """一名候补者的最终状态（按输入顺序返回）。"""

    id: str
    final_status: Literal["waiting", "invited", "confirmed", "expired", "canceled"]


@dataclass(frozen=True)
class TrialResult:
    people: list[PersonResult] = field(default_factory=list)
    invitations: list[Invitation] = field(default_factory=list)


class TrialRejected(Exception):
    """事件流处理中的业务冲突；整批试算必须被拒绝，不返回任何部分结果。

    ``error`` 取值：
    - ``cancel_target_invalid``：取消未知编号（reason=not_found）或
      已退出对象（reason=already_exited）
    - ``confirm_without_invitation``：确认时本人无有效邀请
      （reason 区分 unknown_candidate / not_invited / already_confirmed /
      expired / canceled）
    """

    def __init__(
        self,
        error: str,
        message: str,
        *,
        event_index: int,
        at_minute: int,
        candidate_id: str,
        reason: str | None = None,
    ) -> None:
        self.error = error
        self.event_index = event_index
        self.at_minute = at_minute
        self.candidate_id = candidate_id
        self.reason = reason
        super().__init__(f"{error}: {message}")

    def detail(self) -> dict:
        """供 HTTP 层使用的结构化错误信息。"""
        body: dict[str, object] = {
            "error": self.error,
            "event_index": self.event_index,
            "at_minute": self.at_minute,
            "candidate_id": self.candidate_id,
        }
        if self.reason is not None:
            body["reason"] = self.reason
        return body


def simulate(
    *,
    seats: int,
    hold_minutes: int,
    start_minute: int,
    settle_minute: int,
    candidates: list[CandidateInput],
    events: list[Event],
) -> TrialResult:
    """按整分钟时间轴试算邀请、确认、失效与取消，返回每人最终状态与全部邀请。

    形状 / 取值合法性（席位数、保留时长、编号唯一、报名不晚于起始、事件
    非递减且不越界、settle >= start 等）假定已由调用方校验；本函数只在
    动态处理中遇到业务冲突时抛 :class:`TrialRejected`。
    """
    # 起始邀请顺序：优先级降序、报名时刻升序、编号 Unicode 字典序
    ranked = sorted(
        candidates,
        key=lambda c: (-c.priority, c.registered_minute, c.id),
    )

    status: dict[str, str] = {c.id: WAITING for c in ranked}
    invitation_of: dict[str, Invitation] = {}
    invitations: list[Invitation] = []
    # 候补队列按起始排序保存；已获邀 / 退出者在取用时惰性跳过
    queue: deque[str] = deque(c.id for c in ranked)

    free_seats = seats
    # (失效分钟, 序号, 候补者)；序号仅用于堆的稳定 tie-break
    expiries: list[tuple[int, int, str]] = []
    expiry_seq = count()

    def invite_next(now: int) -> None:
        """把当前全部空位立即邀请给队首仍在候补的人（邀请当分钟发出）。"""
        nonlocal free_seats
        while free_seats > 0 and queue:
            candidate_id = queue[0]
            if status[candidate_id] != WAITING:
                queue.popleft()  # 已获邀或已退出：惰性移除
                continue
            queue.popleft()
            invitation = Invitation(
                candidate_id=candidate_id,
                sent_minute=now,
                deadline_minute=now + hold_minutes,
            )
            invitations.append(invitation)
            invitation_of[candidate_id] = invitation
            status[candidate_id] = INVITED
            free_seats -= 1
            heapq.heappush(
                expiries,
                (invitation.deadline_minute + 1, next(expiry_seq), candidate_id),
            )

    def close_invitation(candidate_id: str, result: str, now: int) -> None:
        """把本人邀请落定为 result（同一条记录原位替换，发出顺序不变）。"""
        previous = invitation_of[candidate_id]
        closed = Invitation(
            candidate_id=candidate_id,
            sent_minute=previous.sent_minute,
            deadline_minute=previous.deadline_minute,
            result=result,
            result_minute=now,
        )
        invitation_of[candidate_id] = closed
        index = next(
            i for i, inv in enumerate(invitations) if inv.candidate_id == candidate_id
        )
        invitations[index] = closed

    # 起始时刻邀请至满额（不足满额则全部发出）
    invite_next(start_minute)

    event_index = 0
    total_events = len(events)

    def next_event_minute() -> int | float:
        if event_index >= total_events:
            return float("inf")
        return events[event_index].at_minute

    def next_expiry_minute() -> int | float:
        # 结算时刻之后的失效不再发生：结算当分钟仍可确认
        if expiries and expiries[0][0] <= settle_minute:
            return expiries[0][0]
        return float("inf")

    while True:
        now = min(next_event_minute(), next_expiry_minute())
        if now == float("inf"):
            break

        # 1) 自动失效先于同刻事件
        while expiries and expiries[0][0] == now:
            _, _, candidate_id = heapq.heappop(expiries)
            invitation = invitation_of.get(candidate_id)
            # 发出后可能已被本人确认 / 取消：仅仍持邀者才在本分钟失效
            if (
                invitation is not None
                and status[candidate_id] == INVITED
                and invitation.result == PENDING
                and invitation.deadline_minute + 1 == now
            ):
                close_invitation(candidate_id, EXPIRED_RESULT, now)
                status[candidate_id] = EXPIRED
                free_seats += 1
                invite_next(now)  # 空位立即邀请下一人（新邀请当分钟发出）

        # 2) 同刻事件按输入顺序处理
        while event_index < total_events and events[event_index].at_minute == now:
            event = events[event_index]
            index = event_index
            candidate_id = event.candidate_id
            event_index += 1

            if event.kind == CONFIRM:
                reason = _confirm_reject_reason(candidate_id, status)
                if reason is not None:
                    raise TrialRejected(
                        "confirm_without_invitation",
                        message=(
                            f"confirm for {candidate_id!r} at minute {now} has no "
                            "valid outstanding invitation; the whole batch is rejected"
                        ),
                        event_index=index,
                        at_minute=now,
                        candidate_id=candidate_id,
                        reason=reason,
                    )
                close_invitation(candidate_id, CONFIRMED_RESULT, now)
                status[candidate_id] = CONFIRMED
            else:
                assert event.kind == CANCEL
                if candidate_id not in status:
                    reason = "not_found"
                elif status[candidate_id] in _EXITED:
                    reason = "already_exited"
                else:
                    reason = None
                if reason is not None:
                    raise TrialRejected(
                        "cancel_target_invalid",
                        message=(
                            f"cancel for {candidate_id!r} at minute {now} targets "
                            f"a candidate that is {reason.replace('_', ' ')}; "
                            "the whole batch is rejected"
                        ),
                        event_index=index,
                        at_minute=now,
                        candidate_id=candidate_id,
                        reason=reason,
                    )
                previous = status[candidate_id]
                status[candidate_id] = CANCELED
                if previous == INVITED:
                    # 取消持邀者：邀请了结为 canceled，席位释放并立即补位
                    close_invitation(candidate_id, CANCELED_RESULT, now)
                    free_seats += 1
                    invite_next(now)
                elif previous == CONFIRMED:
                    # 取消已确认者：邀请结果仍为 confirmed，席位释放并立即补位
                    free_seats += 1
                    invite_next(now)
                # 取消待邀者：仅退出队列（取用时惰性跳过），不释放席位

    people = [
        PersonResult(id=c.id, final_status=status[c.id]) for c in candidates
    ]
    return TrialResult(people=people, invitations=invitations)


def _confirm_reject_reason(
    candidate_id: str, status: dict[str, str]
) -> str | None:
    """确认合法返回 None；否则返回结构化原因。"""
    if candidate_id not in status:
        return "unknown_candidate"
    current = status[candidate_id]
    if current == INVITED:
        return None
    return {
        WAITING: "not_invited",
        CONFIRMED: "already_confirmed",
        EXPIRED: "expired",
        CANCELED: "canceled",
    }[current]
