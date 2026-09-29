"""候补席位试算核心逻辑测试（仅依赖标准库 + pytest）。"""

from __future__ import annotations

import pytest

from app.waitlist import (
    CANCEL,
    CANCELED,
    CANCELED_RESULT,
    CONFIRM,
    CONFIRMED,
    CONFIRMED_RESULT,
    EXPIRED,
    EXPIRED_RESULT,
    INVITED,
    PENDING,
    WAITING,
    CandidateInput,
    Event,
    TrialRejected,
    simulate,
)


def _c(cid: str, priority: int = 0, registered: int = 0) -> CandidateInput:
    return CandidateInput(id=cid, priority=priority, registered_minute=registered)


def _ev(kind: str, minute: int, cid: str) -> Event:
    return Event(at_minute=minute, kind=kind, candidate_id=cid)


def _status(result) -> dict[str, str]:
    return {p.id: p.final_status for p in result.people}


def _inv_map(result) -> dict[str, object]:
    return {inv.candidate_id: inv for inv in result.invitations}


def test_start_invites_to_capacity_by_priority_then_signup_then_id():
    # 2 席；优先级高者先得，平局按报名时刻，再按编号字典序
    result = simulate(
        seats=2,
        hold_minutes=10,
        start_minute=100,
        settle_minute=105,  # 早于首次失效（111）：仅前两名持邀，其余等待
        candidates=[
            _c("lo", 0, 90),
            _c("b-hi-late", 5, 95),
            _c("a-hi-late", 5, 95),  # 与 b 同优先级同报名，编号字典序在前
            _c("mid-early", 5, 90),  # 同优先级、报名更早 -> 在 a/b 之前
        ],
        events=[],
    )
    assert [inv.candidate_id for inv in result.invitations] == [
        "mid-early",
        "a-hi-late",
    ]
    assert _status(result) == {
        "mid-early": INVITED,
        "a-hi-late": INVITED,
        "b-hi-late": WAITING,
        "lo": WAITING,
    }
    inv = _inv_map(result)["mid-early"]
    assert inv.sent_minute == 100
    assert inv.deadline_minute == 110
    assert inv.result == PENDING
    assert inv.result_minute is None


def test_people_returned_in_input_order():
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=5,
        candidates=[_c("z", 0), _c("a", 9)],
        events=[],
    )
    assert [p.id for p in result.people] == ["z", "a"]
    assert _status(result) == {"z": WAITING, "a": INVITED}


def test_confirm_at_deadline_minute_succeeds_next_minute_expires():
    # 邀请 [0 发出, 截止 2]：2 当分钟仍可确认；3 自动失效
    result = simulate(
        seats=2,
        hold_minutes=2,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A"), _c("B")],
        events=[_ev(CONFIRM, 2, "A")],
    )
    inv = _inv_map(result)
    assert inv["A"].result == CONFIRMED_RESULT
    assert inv["A"].result_minute == 2
    assert inv["B"].result == EXPIRED_RESULT
    assert inv["B"].result_minute == 3
    assert _status(result) == {"A": CONFIRMED, "B": EXPIRED}


def test_confirm_after_expiry_minute_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=2,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CONFIRM, 3, "A")],
        )
    exc = exc_info.value
    assert exc.error == "confirm_without_invitation"
    assert exc.reason == "expired"
    assert exc.event_index == 0
    assert exc.at_minute == 3
    assert exc.detail() == {
        "error": "confirm_without_invitation",
        "event_index": 0,
        "at_minute": 3,
        "candidate_id": "A",
        "reason": "expired",
    }


def test_expiry_immediately_invites_next_person_same_minute():
    # A 0 发邀，hold=2 -> 3 失效；同分钟 B 获邀 [3,5)
    result = simulate(
        seats=1,
        hold_minutes=2,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A"), _c("B"), _c("C")],
        events=[],
    )
    inv = _inv_map(result)
    assert inv["A"].result == EXPIRED_RESULT
    assert inv["A"].result_minute == 3
    assert inv["B"].sent_minute == 3
    assert inv["B"].deadline_minute == 5
    assert inv["B"].result == EXPIRED_RESULT
    assert inv["B"].result_minute == 6
    assert inv["C"].sent_minute == 6
    assert inv["C"].result == EXPIRED_RESULT
    assert inv["C"].result_minute == 9
    assert _status(result) == {"A": EXPIRED, "B": EXPIRED, "C": EXPIRED}


def test_expiry_at_settle_minute_still_processed_but_no_confirmation():
    # 0 发邀、hold=2，3 == 结算分钟：自动失效仍发生；3 当分钟的确认先失效后处理
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=2,
            start_minute=0,
            settle_minute=3,
            candidates=[_c("A")],
            events=[_ev(CONFIRM, 3, "A")],
        )
    assert exc_info.value.reason == "expired"


def test_pending_invitation_outliving_settle_remains_pending():
    # hold=10，结算 5：邀请在结算时仍有效，无失效
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=5,
        candidates=[_c("A")],
        events=[],
    )
    assert _status(result) == {"A": INVITED}
    assert result.invitations[0].result == PENDING


def test_confirm_at_deadline_at_settle_minute_is_confirmed():
    result = simulate(
        seats=1,
        hold_minutes=3,
        start_minute=0,
        settle_minute=3,
        candidates=[_c("A")],
        events=[_ev(CONFIRM, 3, "A")],
    )
    assert _status(result) == {"A": CONFIRMED}
    assert result.invitations[0].result == CONFIRMED_RESULT


def test_cancel_waiting_person_only_leaves_queue_no_seat_freed():
    # 1 席 A 持邀，B、C 等待；取消 B（待邀）后即使 A 后续失效，
    # 也应直接邀请 C 而非 B
    result = simulate(
        seats=1,
        hold_minutes=2,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A", 1), _c("B", 0), _c("C", 0)],
        events=[_ev(CANCEL, 1, "B")],
    )
    inv = _inv_map(result)
    assert "B" not in inv  # 待邀取消从未获邀
    assert inv["C"].sent_minute == 3  # A 3 失效后补位
    assert _status(result) == {"A": EXPIRED, "B": CANCELED, "C": EXPIRED}


def test_cancel_invited_holder_closes_invitation_and_fills_immediately():
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A", 1), _c("B", 0)],
        events=[_ev(CANCEL, 4, "A")],
    )
    inv = _inv_map(result)
    assert inv["A"].result == CANCELED_RESULT
    assert inv["A"].result_minute == 4
    assert inv["B"].sent_minute == 4  # 同分钟立即补位
    assert inv["B"].result == PENDING
    assert _status(result) == {"A": CANCELED, "B": INVITED}


def test_cancel_confirmed_person_keeps_invitation_confirmed_and_fills():
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A", 1), _c("B", 0)],
        events=[_ev(CONFIRM, 2, "A"), _ev(CANCEL, 5, "A")],
    )
    inv = _inv_map(result)
    # 邀请事实是“已确认”；取消已确认者释放席位但不改变邀请结果
    assert inv["A"].result == CONFIRMED_RESULT
    assert inv["A"].result_minute == 2
    assert inv["B"].sent_minute == 5
    assert inv["B"].result == PENDING
    assert _status(result) == {"A": CANCELED, "B": INVITED}


def test_expiry_precedes_same_minute_events():
    # hold=2：3 当分钟先自动失效 A，再处理对 B 的同刻邀请确认
    result = simulate(
        seats=1,
        hold_minutes=2,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A", 1), _c("B", 0)],
        events=[_ev(CONFIRM, 3, "B")],
    )
    inv = _inv_map(result)
    assert inv["A"].result == EXPIRED_RESULT
    assert inv["A"].result_minute == 3
    assert inv["B"].sent_minute == 3
    assert inv["B"].result == CONFIRMED_RESULT
    assert inv["B"].result_minute == 3
    assert _status(result) == {"A": EXPIRED, "B": CONFIRMED}


def test_same_minute_events_processed_in_input_order():
    # 1 席：B 先取消（待邀退出），随后 A 取消（持邀，释放）；
    # 补位时跳过已取消的 B，直接邀请 C
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=10,
        candidates=[_c("A", 2), _c("B", 1), _c("C", 0)],
        events=[_ev(CANCEL, 4, "B"), _ev(CANCEL, 4, "A")],
    )
    inv = _inv_map(result)
    assert inv["A"].result == CANCELED_RESULT
    assert "B" not in inv
    assert inv["C"].sent_minute == 4
    assert _status(result) == {"A": CANCELED, "B": CANCELED, "C": INVITED}


def test_confirm_without_any_invitation_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A", 1), _c("B", 0)],
            events=[_ev(CONFIRM, 1, "B")],
        )
    exc = exc_info.value
    assert exc.error == "confirm_without_invitation"
    assert exc.reason == "not_invited"


def test_double_confirm_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CONFIRM, 1, "A"), _ev(CONFIRM, 2, "A")],
        )
    assert exc_info.value.reason == "already_confirmed"
    assert exc_info.value.event_index == 1


def test_confirm_after_self_cancel_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CANCEL, 1, "A"), _ev(CONFIRM, 2, "A")],
        )
    assert exc_info.value.reason == "canceled"


def test_cancel_unknown_candidate_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CANCEL, 1, "X")],
        )
    exc = exc_info.value
    assert exc.error == "cancel_target_invalid"
    assert exc.reason == "not_found"


def test_cancel_already_exited_by_expiry_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=2,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CANCEL, 4, "A")],  # A 已在 3 失效退出
        )
    exc = exc_info.value
    assert exc.error == "cancel_target_invalid"
    assert exc.reason == "already_exited"


def test_cancel_already_canceled_rejected():
    with pytest.raises(TrialRejected) as exc_info:
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CANCEL, 1, "A"), _ev(CANCEL, 2, "A")],
        )
    assert exc_info.value.reason == "already_exited"
    assert exc_info.value.event_index == 1


def test_rejection_returns_no_partial_result():
    # 异常抛出，调用方拿不到任何结果对象
    with pytest.raises(TrialRejected):
        simulate(
            seats=1,
            hold_minutes=10,
            start_minute=0,
            settle_minute=10,
            candidates=[_c("A")],
            events=[_ev(CONFIRM, 1, "A"), _ev(CANCEL, 5, "X")],
        )


def test_capacity_chain_confirm_expire_and_cancel_fill():
    # 2 席：A、B 先持邀；
    # A 在 1 确认；B 在 2 取消 -> C 2 获邀；C 在 4（deadline=4）确认；
    # A 在 5 取消 -> D 5 获邀；结算 12 < 15，D 保持持邀
    result = simulate(
        seats=2,
        hold_minutes=10,
        start_minute=0,
        settle_minute=12,
        candidates=[_c("A", 4), _c("B", 3), _c("C", 2), _c("D", 1)],
        events=[
            _ev(CONFIRM, 1, "A"),
            _ev(CANCEL, 2, "B"),
            _ev(CONFIRM, 4, "C"),
            _ev(CANCEL, 5, "A"),
        ],
    )
    inv = _inv_map(result)
    assert [inv.candidate_id for inv in result.invitations] == ["A", "B", "C", "D"]
    assert inv["A"].result == CONFIRMED_RESULT
    assert inv["B"].result == CANCELED_RESULT
    assert inv["B"].result_minute == 2
    assert inv["C"].sent_minute == 2
    assert inv["C"].result == CONFIRMED_RESULT
    assert inv["D"].sent_minute == 5
    assert inv["D"].deadline_minute == 15
    assert inv["D"].result == PENDING
    assert _status(result) == {
        "A": CANCELED,
        "B": CANCELED,
        "C": CONFIRMED,
        "D": INVITED,
    }


def test_more_seats_than_candidates_everyone_invited():
    result = simulate(
        seats=5,
        hold_minutes=10,
        start_minute=0,
        settle_minute=3,
        candidates=[_c("A"), _c("B")],
        events=[],
    )
    assert [p.final_status for p in result.people] == [INVITED, INVITED]
    assert len(result.invitations) == 2


def test_settle_equals_start_with_start_minute_event():
    # 结算 == 起始：起始邀请先建立，起始分钟的事件正常处理
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=100,
        settle_minute=100,
        candidates=[_c("A")],
        events=[_ev(CONFIRM, 100, "A")],
    )
    assert _status(result) == {"A": CONFIRMED}


def test_no_events_valid_standalone_trial():
    result = simulate(
        seats=1,
        hold_minutes=10,
        start_minute=0,
        settle_minute=5,
        candidates=[_c("A", 1), _c("B", 0)],
        events=[],
    )
    assert _status(result) == {"A": INVITED, "B": WAITING}
