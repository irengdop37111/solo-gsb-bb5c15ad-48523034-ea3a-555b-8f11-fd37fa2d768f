"""FEFO 库存分配核心逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import date
from decimal import Decimal

import pytest

from app.inventory import (
    BatchInput,
    InsufficientStock,
    OrderInput,
    allocate,
)


def _batch(no: str, qty: str, expiry: str) -> BatchInput:
    return BatchInput(no, Decimal(qty), date.fromisoformat(expiry))


def _order(oid: str, qty: str) -> OrderInput:
    return OrderInput(oid, Decimal(qty))


def test_fefo_order_by_expiry_then_batch_no():
    # 同日批次按批次号字典序：B02 < B10 < B9
    allocations, remaining, total = allocate(
        batches=[
            _batch("B9", "100", "2026-06-01"),
            _batch("B10", "3", "2026-03-01"),
            _batch("B02", "5", "2026-03-01"),
            _batch("B01", "7", "2026-01-01"),
        ],
        orders=[_order("O1", "10")],
    )
    lines = allocations[0].lines
    assert [(l.batch_no, l.quantity) for l in lines] == [
        ("B01", Decimal("7")),
        ("B02", Decimal("3")),
    ]
    assert total == Decimal("10")
    by_no = {r.batch_no: r.remaining for r in remaining}
    assert by_no["B01"] == 0
    assert by_no["B02"] == Decimal("2")
    assert by_no["B10"] == Decimal("3")
    assert by_no["B9"] == Decimal("100")
    # 剩余库存表按 FEFO 顺序排列
    assert [r.batch_no for r in remaining] == ["B01", "B02", "B10", "B9"]


def test_order_split_across_batches_with_decimal_quantities():
    allocations, remaining, total = allocate(
        batches=[
            _batch("A", "2.5", "2026-01-01"),
            _batch("B", "3.25", "2026-02-01"),
        ],
        orders=[_order("O1", "5.75")],
    )
    assert [(l.batch_no, l.quantity) for l in allocations[0].lines] == [
        ("A", Decimal("2.5")),
        ("B", Decimal("3.25")),
    ]
    assert total == Decimal("5.75")
    assert all(r.remaining == 0 for r in remaining)
    # 每单明细之和等于订单需求
    assert sum((l.quantity for l in allocations[0].lines), Decimal(0)) == Decimal(
        "5.75"
    )


def test_multiple_orders_processed_in_input_order_and_share_stock():
    allocations, remaining, total = allocate(
        batches=[
            _batch("A", "5", "2026-01-01"),
            _batch("B", "5", "2026-02-01"),
        ],
        orders=[_order("O1", "7"), _order("O2", "3")],
    )
    assert [(l.batch_no, l.quantity) for l in allocations[0].lines] == [
        ("A", Decimal("5")),
        ("B", Decimal("2")),
    ]
    assert [(l.batch_no, l.quantity) for l in allocations[1].lines] == [
        ("B", Decimal("3"))
    ]
    assert total == Decimal("10")
    assert all(r.remaining == 0 for r in remaining)


def test_exhausted_batches_are_skipped_for_later_orders():
    allocations, _, _ = allocate(
        batches=[_batch("A", "1", "2026-01-01"), _batch("B", "10", "2026-02-01")],
        orders=[_order("O1", "1"), _order("O2", "2")],
    )
    assert [(l.batch_no, l.quantity) for l in allocations[1].lines] == [
        ("B", Decimal("2"))
    ]


def test_insufficient_stock_raises_and_carries_detail():
    with pytest.raises(InsufficientStock) as exc_info:
        allocate(
            batches=[
                _batch("A", "5", "2026-01-01"),
                _batch("B", "3", "2026-02-01"),
            ],
            orders=[_order("O1", "4"), _order("O2", "10")],
        )
    exc = exc_info.value
    assert exc.order_id == "O2"
    assert exc.requested == Decimal("10")
    # O1 已占用 4，剩余 4，O2 缺 6
    assert exc.shortfall == Decimal("6")
    assert exc.total_available == Decimal("8")
    detail = exc.detail()
    assert detail == {
        "error": "insufficient_stock",
        "order_id": "O2",
        "requested": 10,
        "shortfall": 6,
        "total_available": 8,
    }


def test_insufficient_stock_when_total_exceeds_stock_even_if_first_order_fails():
    with pytest.raises(InsufficientStock) as exc_info:
        allocate(
            batches=[_batch("A", "3", "2026-01-01")],
            orders=[_order("O1", "4")],
        )
    assert exc_info.value.shortfall == Decimal("1")


def test_available_before_reflects_input_not_post_allocation():
    _, remaining, _ = allocate(
        batches=[_batch("A", "5", "2026-01-01"), _batch("B", "7", "2026-02-01")],
        orders=[_order("O1", "6")],
    )
    assert [(r.batch_no, r.available_before, r.remaining) for r in remaining] == [
        ("A", Decimal("5"), Decimal("0")),
        ("B", Decimal("7"), Decimal("6")),
    ]


def test_integer_quantities_serialize_as_int():
    allocations, _, total = allocate(
        batches=[_batch("A", "5", "2026-01-01")],
        orders=[_order("O1", "5")],
    )
    assert total == 5  # Decimal(5) == 5
    assert allocations[0].lines[0].quantity == Decimal("5")
