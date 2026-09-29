"""收款核销试算核心逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import date
from decimal import Decimal

import pytest

from app.receivables import (
    Event,
    InvoiceInput,
    WriteOffRejected,
    as_number,
    simulate,
)


def _inv(no: str, due: str, amount: str) -> InvoiceInput:
    return InvoiceInput(no, date.fromisoformat(due), Decimal(amount))


def _receipt(eid: str, amount: str) -> Event:
    return Event(kind="receipt", event_id=eid, amount=Decimal(amount))


def _reversal(eid: str, target: str) -> Event:
    return Event(kind="reversal", event_id=eid, reversal_of=target)


def _lines(result, event_id):
    rec = next(r for r in result.receipts if r.event_id == event_id)
    return [(l.invoice_no, l.amount) for l in rec.lines]


def test_invoices_ordered_by_due_date_then_invoice_no():
    # 到期日升序；同日按发票号 Unicode 字典序：INV-001 < INV-002 < INV-010
    invoices = [
        _inv("INV-010", "2026-02-01", "10"),
        _inv("INV-002", "2026-01-01", "7"),
        _inv("INV-001", "2026-01-01", "5"),
    ]
    result = simulate(invoices, [_receipt("R1", "20")])
    assert _lines(result, "R1") == [
        ("INV-001", Decimal("5")),
        ("INV-002", Decimal("7")),
        ("INV-010", Decimal("8")),
    ]
    # 未结表按 (到期日, 发票号) 排序
    assert [inv.invoice_no for inv in result.invoices] == [
        "INV-001",
        "INV-002",
        "INV-010",
    ]
    outstanding = {inv.invoice_no: inv.outstanding for inv in result.invoices}
    assert outstanding == {"INV-001": 0, "INV-002": 0, "INV-010": Decimal("2")}
    assert result.total_written_off == Decimal("20")


def test_receipt_split_and_receipts_share_outstanding_in_order():
    invoices = [_inv("A", "2026-01-01", "10"), _inv("B", "2026-02-01", "10")]
    result = simulate(invoices, [_receipt("R1", "8"), _receipt("R2", "7")])
    assert _lines(result, "R1") == [("A", Decimal("8"))]
    assert _lines(result, "R2") == [
        ("A", Decimal("2")),
        ("B", Decimal("5")),
    ]
    assert {inv.invoice_no: inv.outstanding for inv in result.invoices} == {
        "A": Decimal("0"),
        "B": Decimal("5"),
    }
    assert result.total_written_off == Decimal("15")


def test_exact_equal_receipt_settles_all_invoices():
    invoices = [_inv("A", "2026-01-01", "2.50"), _inv("B", "2026-02-01", "3.25")]
    result = simulate(invoices, [_receipt("R1", "5.75")])
    assert all(inv.outstanding == 0 for inv in result.invoices)
    assert result.total_written_off == Decimal("5.75")


def test_receipt_exceeding_remaining_rejected_without_partial_result():
    invoices = [_inv("A", "2026-01-01", "5")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_receipt("R1", "5.01")])
    exc = exc_info.value
    assert exc.error == "receipt_exceeds_receivable"
    assert exc.event_id == "R1"
    assert exc.detail() == {
        "error": "receipt_exceeds_receivable",
        "event_id": "R1",
        "amount": 5.01,
        "remaining_receivable": 5,
        "shortfall": 0.01,
    }


def test_exceed_is_checked_against_current_remaining_after_earlier_receipts():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_receipt("R1", "10"), _receipt("R2", "0.01")])
    assert exc_info.value.error == "receipt_exceeds_receivable"
    assert exc_info.value.event_id == "R2"


def test_withdrawal_releases_exactly_original_lines_for_later_receipt():
    # R1 核销 A:10；R2 核销 B:5；撤回 R1 只释放 A，不重排 R2；
    # R3 只能使用释放出的 A 的 10 额度
    invoices = [_inv("A", "2026-01-01", "10"), _inv("B", "2026-02-01", "10")]
    result = simulate(
        invoices,
        [
            _receipt("R1", "10"),
            _receipt("R2", "5"),
            _reversal("V1", "R1"),
            _receipt("R3", "10"),
        ],
    )
    # 有效收款不含 R1
    assert [r.event_id for r in result.receipts] == ["R2", "R3"]
    assert _lines(result, "R2") == [("B", Decimal("5"))]
    assert _lines(result, "R3") == [("A", Decimal("10"))]
    assert {inv.invoice_no: inv.outstanding for inv in result.invoices} == {
        "A": Decimal("0"),
        "B": Decimal("5"),
    }
    assert result.total_written_off == Decimal("15")


def test_withdrawal_restores_split_lines_across_invoices():
    # R1 跨三张发票核销；撤回后逐笔加回，未结恢复到核销前
    invoices = [
        _inv("A", "2026-01-01", "5"),
        _inv("B", "2026-01-02", "5"),
        _inv("C", "2026-01-03", "5"),
    ]
    result = simulate(invoices, [_receipt("R1", "12"), _reversal("V1", "R1")])
    assert result.receipts == []
    assert {inv.invoice_no: inv.outstanding for inv in result.invoices} == {
        "A": Decimal("5"),
        "B": Decimal("5"),
        "C": Decimal("5"),
    }
    assert result.total_written_off == 0


def test_withdrawal_frees_capacity_making_later_oversized_receipt_fit():
    invoices = [_inv("A", "2026-01-01", "10")]
    # R1=10 结清；若无撤回，R2=10 会超额；撤回 R1 后 R2 可全额核销
    result = simulate(
        invoices,
        [_receipt("R1", "10"), _reversal("V1", "R1"), _receipt("R2", "10")],
    )
    assert [r.event_id for r in result.receipts] == ["R2"]
    assert _lines(result, "R2") == [("A", Decimal("10"))]
    assert result.invoices[0].outstanding == 0


def test_identical_receipt_replay_is_counted_once():
    invoices = [_inv("A", "2026-01-01", "10")]
    result = simulate(
        invoices, [_receipt("R1", "8"), _receipt("R1", "8"), _receipt("R2", "2")]
    )
    # R1 只核销一次（8），R2 才得以用剩余 2 结清
    assert [r.event_id for r in result.receipts] == ["R1", "R2"]
    assert _lines(result, "R1") == [("A", Decimal("8"))]
    assert _lines(result, "R2") == [("A", Decimal("2"))]
    assert result.total_written_off == Decimal("10")


def test_identical_reversal_replay_is_counted_once():
    invoices = [_inv("A", "2026-01-01", "10")]
    # 第二笔撤回是同一事件 V1 的完全一致重放：跳过，不当作二次撤回
    result = simulate(
        invoices,
        [
            _receipt("R1", "5"),
            _reversal("V1", "R1"),
            _reversal("V1", "R1"),
            _receipt("R2", "5"),
        ],
    )
    assert [r.event_id for r in result.receipts] == ["R2"]
    assert result.invoices[0].outstanding == Decimal("5")


def test_replay_with_different_amount_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_receipt("R1", "8"), _receipt("R1", "9")])
    assert exc_info.value.error == "inconsistent_replay"
    assert exc_info.value.event_id == "R1"


def test_replay_with_different_reversal_target_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(
            invoices,
            [
                _receipt("R1", "1"),
                _receipt("R2", "1"),
                _reversal("V1", "R1"),
                _reversal("V1", "R2"),
            ],
        )
    assert exc_info.value.error == "inconsistent_replay"
    assert exc_info.value.event_id == "V1"


def test_replay_switching_type_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    # 同事件号先收款后撤回（撤回还指向不存在的事件），内容不同
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(
            invoices,
            [_receipt("E1", "8"), _reversal("E1", "X")],
        )
    assert exc_info.value.error == "inconsistent_replay"

    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(
            invoices,
            [_reversal("E1", "X"), _receipt("E1", "8")],
        )
    # 首笔撤回本身目标不存在，先报目标非法
    assert exc_info.value.error == "reversal_target_invalid"


def test_reversal_of_nonexistent_receipt_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_reversal("V1", "R1")])
    assert exc_info.value.error == "reversal_target_invalid"
    assert exc_info.value.detail() == {
        "error": "reversal_target_invalid",
        "event_id": "V1",
        "referenced_event_id": "R1",
        "reason": "not_found",
    }


def test_reversal_targeting_a_reversal_event_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(
            invoices,
            [
                _receipt("R1", "1"),
                _reversal("V1", "R1"),
                _reversal("V2", "V1"),
            ],
        )
    assert exc_info.value.error == "reversal_target_invalid"
    assert exc_info.value.detail()["reason"] == "not_found"


def test_reversal_of_already_withdrawn_receipt_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(
            invoices,
            [
                _receipt("R1", "1"),
                _reversal("V1", "R1"),
                _reversal("V2", "R1"),
            ],
        )
    assert exc_info.value.error == "reversal_target_invalid"
    assert exc_info.value.event_id == "V2"
    assert exc_info.value.detail()["reason"] == "already_reversed"


def test_forward_reversal_reference_rejected_even_if_receipt_later():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_reversal("V1", "R1"), _receipt("R1", "1")])
    assert exc_info.value.error == "reversal_target_invalid"
    assert exc_info.value.detail()["reason"] == "not_found"


def test_self_referencing_reversal_rejected():
    invoices = [_inv("A", "2026-01-01", "10")]
    with pytest.raises(WriteOffRejected) as exc_info:
        simulate(invoices, [_reversal("V1", "V1")])
    assert exc_info.value.error == "reversal_target_invalid"


def test_cents_precision_throughout():
    invoices = [_inv("A", "2026-01-01", "0.10"), _inv("B", "2026-01-02", "0.20")]
    result = simulate(
        invoices,
        [_receipt("R1", "0.15"), _receipt("R2", "0.15")],
    )
    assert _lines(result, "R1") == [
        ("A", Decimal("0.10")),
        ("B", Decimal("0.05")),
    ]
    assert _lines(result, "R2") == [("B", Decimal("0.15"))]
    assert {inv.invoice_no: inv.outstanding for inv in result.invoices} == {
        "A": Decimal("0.00"),
        "B": Decimal("0.00"),
    }
    assert result.total_written_off == Decimal("0.30")


def test_receivable_rows_keep_original_receivable():
    invoices = [_inv("A", "2026-01-01", "12.34")]
    result = simulate(invoices, [_receipt("R1", "0.01"), _reversal("V1", "R1")])
    row = result.invoices[0]
    assert row.receivable == Decimal("12.34")
    assert row.outstanding == Decimal("12.34")


def test_as_number_int_vs_float():
    assert as_number(Decimal("5")) == 5 and isinstance(as_number(Decimal("5")), int)
    assert as_number(Decimal("5.5")) == 5.5
    assert as_number(Decimal("0")) == 0
