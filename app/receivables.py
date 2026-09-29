"""收款对发票的核销试算逻辑。

规则：
- 发票按到期日从早到晚排序；同日按发票号 Unicode 字典序排序。
- 事件按输入顺序处理。收款依次填补排好序的发票未结清金额，单笔收款
  可拆分核销到多张发票；**收款额严格大于当时全部发票剩余应收合计时
  整批拒绝**（恰好相等允许，发票恰好结清）。
- 撤回只释放目标收款**原先核销的那些金额**（按原明细逐笔加回），
  已处理的其他收款保持不动、不重新分配；释放出的额度可供后续收款使用。
- 幂等重放：相同事件号且内容完全一致的事件只生效一次（直接跳过，
  不再次核销 / 不再次释放）；相同事件号但内容不同（含收款与撤回
  类型互换、金额不一致、撤回目标不一致）整批拒绝。
- 撤回目标收款事件在该撤回之前不存在（事件号不存在、指向撤回事件、
  指向尚未出现的后续事件），或已经被撤回，均整批拒绝。

任何拒绝都抛出 :class:`WriteOffRejected`，本模块不“提交”任何中间结果，
调用方据此整批拒绝、绝不返回部分试算。全部金额使用 ``Decimal`` 精确运算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

__all__ = [
    "InvoiceInput",
    "Event",
    "WriteOffLine",
    "ReceiptWriteOff",
    "InvoiceOutstanding",
    "WriteOffResult",
    "WriteOffRejected",
    "simulate",
    "as_number",
]

# 事件类型
RECEIPT: Literal["receipt"] = "receipt"
REVERSAL: Literal["reversal"] = "reversal"


@dataclass(frozen=True)
class InvoiceInput:
    invoice_no: str
    due_date: date
    receivable: Decimal


@dataclass(frozen=True)
class Event:
    """按顺序出现的一个事件：收款或撤回。

    收款（``kind="receipt"``）携带 ``amount``；
    撤回（``kind="reversal"``）携带 ``reversal_of``（先前收款事件号）。
    """

    kind: Literal["receipt", "reversal"]
    event_id: str
    amount: Decimal | None = None
    reversal_of: str | None = None


@dataclass(frozen=True)
class WriteOffLine:
    """一笔收款在某张发票上的一次核销。"""

    invoice_no: str
    due_date: date
    amount: Decimal


@dataclass(frozen=True)
class ReceiptWriteOff:
    event_id: str
    amount: Decimal  # 收款原始金额
    lines: list[WriteOffLine]  # 按 (到期日, 发票号) 填补顺序排列


@dataclass(frozen=True)
class InvoiceOutstanding:
    invoice_no: str
    due_date: date
    receivable: Decimal  # 发票原始应收
    outstanding: Decimal  # 试算后未结金额


@dataclass(frozen=True)
class WriteOffResult:
    receipts: list[ReceiptWriteOff] = field(default_factory=list)
    invoices: list[InvoiceOutstanding] = field(default_factory=list)
    total_written_off: Decimal = Decimal(0)


class WriteOffRejected(Exception):
    """整批试算必须被拒绝的业务冲突；不允许返回任何部分结果。

    ``error`` 取值：
    - ``inconsistent_replay``：相同事件号内容不一致（金额 / 类型 / 撤回目标不同）
    - ``receipt_exceeds_receivable``：收款超过当时剩余应收合计
    - ``reversal_target_invalid``：撤回目标不存在或已撤回
    """

    def __init__(
        self,
        error: str,
        event_id: str,
        message: str,
        **extra: object,
    ) -> None:
        self.error = error
        self.event_id = event_id
        self.message = message
        self.extra = extra
        super().__init__(f"{error}: {message}")

    def detail(self) -> dict:
        """供 HTTP 层使用的结构化错误信息（Decimal 转为 JSON 数字）。"""
        body: dict[str, object] = {"error": self.error, "event_id": self.event_id}
        for key, value in self.extra.items():
            body[key] = as_number(value) if isinstance(value, Decimal) else value
        return body


def as_number(value: Decimal) -> int | float:
    """Decimal 转 JSON 数字：整数取值返回 int，否则返回 float。"""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def simulate(
    invoices: list[InvoiceInput],
    events: list[Event],
) -> WriteOffResult:
    """按顺序试算收款与撤回，返回核销明细与各发票未结金额。

    任一业务冲突抛 :class:`WriteOffRejected`，不返回任何部分结果。
    """
    # 发票按 (到期日, 发票号 Unicode 字典序) 稳定排序
    ordered_invoices = sorted(
        invoices, key=lambda inv: (inv.due_date, inv.invoice_no)
    )
    outstanding = {inv.invoice_no: inv.receivable for inv in ordered_invoices}

    # event_id -> 首次出现时的内容签名，用于幂等重放判定
    seen_signature: dict[str, tuple[object, ...]] = {}
    # 生效中的收款事件号 -> 原核销明细（撤回后弹出）
    active_lines: dict[str, list[WriteOffLine]] = {}
    withdrawn: set[str] = set()
    receipts: list[ReceiptWriteOff] = []

    for ev in events:
        signature = _signature(ev)
        if ev.event_id in seen_signature:
            if seen_signature[ev.event_id] != signature:
                # 相同事件号内容不同：整批拒绝
                raise WriteOffRejected(
                    "inconsistent_replay",
                    event_id=ev.event_id,
                    message=(
                        f"event {ev.event_id!r} was replayed with different "
                        "content; the whole batch is rejected"
                    ),
                )
            # 内容完全一致的重放：只计一次，直接跳过
            continue
        seen_signature[ev.event_id] = signature

        if ev.kind == RECEIPT:
            assert ev.amount is not None
            remaining_total = sum(outstanding.values(), Decimal(0))
            if ev.amount > remaining_total:
                # 收款超过剩余应收：整批拒绝，丢弃此前全部模拟核销
                raise WriteOffRejected(
                    "receipt_exceeds_receivable",
                    event_id=ev.event_id,
                    message=(
                        f"receipt {ev.event_id!r} amount {ev.amount} exceeds "
                        f"remaining receivable {remaining_total}"
                    ),
                    amount=ev.amount,
                    remaining_receivable=remaining_total,
                    shortfall=ev.amount - remaining_total,
                )

            need = ev.amount
            lines: list[WriteOffLine] = []
            for inv in ordered_invoices:
                if need == 0:
                    break
                available = outstanding[inv.invoice_no]
                if available <= 0:
                    continue
                take = min(need, available)
                lines.append(
                    WriteOffLine(
                        invoice_no=inv.invoice_no,
                        due_date=inv.due_date,
                        amount=take,
                    )
                )
                outstanding[inv.invoice_no] = available - take
                need -= take
            active_lines[ev.event_id] = lines
            receipts.append(
                ReceiptWriteOff(event_id=ev.event_id, amount=ev.amount, lines=lines)
            )
        else:
            # 撤回：只释放目标收款原先核销的金额，其他收款不重新分配
            assert ev.reversal_of is not None
            target = ev.reversal_of
            if target not in active_lines:
                reason = (
                    "already_reversed" if target in withdrawn else "not_found"
                )
                raise WriteOffRejected(
                    "reversal_target_invalid",
                    event_id=ev.event_id,
                    message=(
                        f"reversal {ev.event_id!r} references receipt {target!r} "
                        f"that is {reason.replace('_', ' ')}; "
                        "the whole batch is rejected"
                    ),
                    referenced_event_id=target,
                    reason=reason,
                )
            for line in active_lines.pop(target):
                outstanding[line.invoice_no] += line.amount
            withdrawn.add(target)

    # 已撤回的收款不再属于“有效收款”，从明细中剔除（其金额已全部释放）
    effective_receipts = [r for r in receipts if r.event_id not in withdrawn]
    invoice_rows = [
        InvoiceOutstanding(
            invoice_no=inv.invoice_no,
            due_date=inv.due_date,
            receivable=inv.receivable,
            outstanding=outstanding[inv.invoice_no],
        )
        for inv in ordered_invoices
    ]
    total_written_off = sum(
        (line.amount for r in effective_receipts for line in r.lines),
        Decimal(0),
    )
    return WriteOffResult(
        receipts=effective_receipts,
        invoices=invoice_rows,
        total_written_off=total_written_off,
    )


def _signature(ev: Event) -> tuple[object, ...]:
    """事件内容签名：事件号相同且签名相同才算“内容完全一致”的重放。"""
    if ev.kind == RECEIPT:
        return (RECEIPT, ev.amount)
    return (REVERSAL, ev.reversal_of)
