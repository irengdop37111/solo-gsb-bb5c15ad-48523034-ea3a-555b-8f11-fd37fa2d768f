"""库存批次 FEFO（First-Expired-First-Out，先到期先出）分配逻辑。

规则：
- 批次按失效日期从早到晚排序；同日按批次号 Unicode 字典序排序。
- 订单按输入顺序逐条满足，单条订单可拆分到多个批次。
- 每个订单必须足量满足后整批结果才算成功；任一订单库存不足即抛
  ``InsufficientStock``，本模块不做任何“提交”，调用方据此整批拒绝、
  绝不返回部分分配。

全部数量使用 ``Decimal`` 精确运算，避免浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

__all__ = [
    "BatchInput",
    "OrderInput",
    "AllocationLine",
    "OrderAllocation",
    "BatchRemaining",
    "InsufficientStock",
    "allocate",
    "as_number",
]


@dataclass(frozen=True)
class BatchInput:
    batch_no: str
    available_quantity: Decimal
    expiry_date: date


@dataclass(frozen=True)
class OrderInput:
    order_id: str
    quantity: Decimal


@dataclass(frozen=True)
class AllocationLine:
    """单条订单在某个批次上的一次分配。"""

    batch_no: str
    expiry_date: date
    quantity: Decimal


@dataclass(frozen=True)
class OrderAllocation:
    order_id: str
    quantity: Decimal  # 订单原始需求数量
    lines: list[AllocationLine]  # 按 FEFO 取用顺序排列


@dataclass(frozen=True)
class BatchRemaining:
    batch_no: str
    expiry_date: date
    available_before: Decimal  # 分配前可用量
    remaining: Decimal  # 分配后剩余量


class InsufficientStock(Exception):
    """库存不足以足量满足某条订单，整批分配必须被拒绝。"""

    def __init__(
        self,
        order_id: str,
        requested: Decimal,
        shortfall: Decimal,
        total_available: Decimal,
    ) -> None:
        self.order_id = order_id
        self.requested = requested
        self.shortfall = shortfall
        self.total_available = total_available
        super().__init__(
            f"insufficient stock for order {order_id!r}: "
            f"requested {requested}, shortfall {shortfall}, "
            f"total available across batches {total_available}"
        )

    def detail(self) -> dict:
        """供 HTTP 层使用的结构化错误信息（数量转为 JSON 数字）。"""
        return {
            "error": "insufficient_stock",
            "order_id": self.order_id,
            "requested": as_number(self.requested),
            "shortfall": as_number(self.shortfall),
            "total_available": as_number(self.total_available),
        }


def as_number(value: Decimal) -> int | float:
    """Decimal 转 JSON 数字：整数取值返回 int，否则返回 float。"""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def allocate(
    batches: list[BatchInput],
    orders: list[OrderInput],
) -> tuple[list[OrderAllocation], list[BatchRemaining], Decimal]:
    """执行 FEFO 分配。

    返回 ``(每单分配明细, 各批次剩余库存, 总分配量)``；
    任一订单无法足量满足时抛 :class:`InsufficientStock`，不返回任何部分结果。
    """
    # FEFO：失效日期升序，同日批次号字典序（Unicode 码位）升序
    ordered_batches = sorted(
        batches, key=lambda b: (b.expiry_date, b.batch_no)
    )
    remaining = {b.batch_no: b.available_quantity for b in ordered_batches}
    total_available = sum((b.available_quantity for b in batches), Decimal(0))

    allocations: list[OrderAllocation] = []
    for order in orders:
        need = order.quantity
        lines: list[AllocationLine] = []
        for batch in ordered_batches:
            if need == 0:
                break
            stock = remaining[batch.batch_no]
            if stock <= 0:
                continue
            take = min(need, stock)
            lines.append(
                AllocationLine(
                    batch_no=batch.batch_no,
                    expiry_date=batch.expiry_date,
                    quantity=take,
                )
            )
            remaining[batch.batch_no] = stock - take
            need -= take
        if need > 0:
            # 不提交任何已模拟的扣减：本服务无状态、调用方也不会写出部分结果
            raise InsufficientStock(
                order_id=order.order_id,
                requested=order.quantity,
                shortfall=need,
                total_available=total_available,
            )
        allocations.append(
            OrderAllocation(
                order_id=order.order_id, quantity=order.quantity, lines=lines
            )
        )

    remaining_rows = [
        BatchRemaining(
            batch_no=batch.batch_no,
            expiry_date=batch.expiry_date,
            available_before=batch.available_quantity,
            remaining=remaining[batch.batch_no],
        )
        for batch in ordered_batches
    ]
    total_allocated = sum(
        (line.quantity for a in allocations for line in a.lines), Decimal(0)
    )
    return allocations, remaining_rows, total_allocated
