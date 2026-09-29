"""候补席位试算核心逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.waitlist import (
    CandidateInput,
    TrialEvent,
    WaitlistRejected,
    simulate,
)

T0 = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> datetime:
    """相对起始时刻 T0 的整分钟时刻。"""
    return T0 + timedelta(minutes=minutes)


def _cand(cid: str, priority: int = 0, registered: int = -60) -> CandidateInput:
    return CandidateInput(cid, priority, _at(registered))


def _events(*specs: tuple[str, str, int]) -> list[TrialEvent]:
    """按 (kind, candidate_id, 相对分钟) 构造事件，index 即输入顺序。"""
    return [
        TrialEvent(kind, cid, _at(at), i)
        for i, (kind, cid, at) in enumerate(specs)
    ]


def _run(
    *,
    seats: int = 1,
    ttl: int = 30,
    settle: int = 240,
    candidates: list[CandidateInput],
    events: list[TrialEvent] | None = None,
):
    return simulate(
        seats=seats,
        start_at=T0,
        ttl_minutes=ttl,
        settle_at=_at(settle),
        candidates=candidates,
        events=list(events or []),
    )


def _statuses(result) -> dict[str, str]:
    return {o.candidate_id: o.status for o in result.candidates}


def _inv_rows(result):
    return [
        (inv.candidate_id, inv.issued_at, inv.deadline, inv.result, inv.resolved_at)
        for inv in result.invitations
    ]


def test_initial_fill_by_priority_then_registration_then_id():
    # 候补顺序：优先级降序 -> 报名时刻升序 -> 编号字典序
    candidates = [
        _cand("b", priority=1, registered=-50),
        _cand("a", priority=1, registered=-50),  # 与 b 同优先级同报名，编号小者先
        _cand("c", priority=2, registered=-10),  # 优先级最高，报名晚仍最先
        _cand("d", priority=0, registered=-100),
    ]
    result = _run(seats=2, settle=20, candidates=candidates)
    assert [inv.candidate_id for inv in result.invitations] == ["c", "a"]
    assert all(inv.issued_at == T0 for inv in result.invitations)
    assert _statuses(result) == {
        "c": "invited",
        "a": "invited",
        "b": "waiting",
        "d": "waiting",
    }


def test_invitation_deadline_and_confirm_on_deadline_minute():
    result = _run(
        seats=1,
        ttl=30,
        candidates=[_cand("x")],
        events=_events(("confirm", "x", 30)),  # 截止当分钟仍可确认
    )
    inv = result.invitations[0]
    assert (inv.issued_at, inv.deadline) == (T0, _at(30))
    assert inv.result == "confirmed"
    assert inv.resolved_at == _at(30)
    assert _statuses(result) == {"x": "confirmed"}


def test_confirm_at_issue_minute_valid():
    result = _run(
        seats=1,
        candidates=[_cand("x")],
        events=_events(("confirm", "x", 0)),
    )
    assert result.invitations[0].result == "confirmed"
    assert _statuses(result) == {"x": "confirmed"}


def test_auto_expiry_minute_after_deadline_and_immediate_refill():
    # x 的邀请 10:00 发出、10:30 截止、10:31 自动失效；y 同分钟补位
    result = _run(seats=1, ttl=30, candidates=[_cand("x"), _cand("y")])
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "expired", _at(31)),
        ("y", _at(31), _at(61), "expired", _at(62)),
    ]
    assert _statuses(result) == {"x": "expired", "y": "expired"}


def test_expiry_processed_before_same_minute_events():
    # 10:31 x 自动失效先于同刻确认事件 -> x 的确认无有效邀请，整批拒绝
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            ttl=30,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("confirm", "x", 31)),
        )
    exc = exc_info.value
    assert exc.error == "no_valid_invitation"
    assert exc.reason == "already_exited"
    assert exc.event_index == 0


def test_expiry_refill_then_same_minute_confirm_of_next():
    # 10:31 x 失效、y 补位；y 在同分钟（按输入顺序在其后）确认 -> 生效
    result = _run(
        seats=1,
        ttl=30,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("confirm", "y", 31)),
    )
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "expired", _at(31)),
        ("y", _at(31), _at(61), "confirmed", _at(31)),
    ]
    assert _statuses(result) == {"x": "expired", "y": "confirmed"}


def test_cancel_waiting_exits_without_releasing_seat():
    result = _run(
        seats=1,
        settle=20,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("cancel", "y", 10)),
    )
    # y 待邀退出，不释放席位、不产生新邀请；x 的邀请不撤换、保持待决
    assert len(result.invitations) == 1
    assert result.invitations[0].candidate_id == "x"
    assert result.invitations[0].result == "pending"
    assert _statuses(result) == {"x": "invited", "y": "cancelled"}


def test_cancel_invited_releases_seat_and_refills_same_minute():
    result = _run(
        seats=1,
        ttl=30,
        settle=20,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("cancel", "x", 10)),
    )
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "cancelled", _at(10)),
        ("y", _at(10), _at(40), "pending", None),
    ]
    assert _statuses(result) == {"x": "cancelled", "y": "invited"}


def test_cancel_confirmed_releases_seat_and_refills_same_minute():
    result = _run(
        seats=1,
        ttl=30,
        settle=20,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("confirm", "x", 5), ("cancel", "x", 10)),
    )
    # x 的邀请结果为已确认（其后的取消不改变邀请结果），y 于 10:10 补位
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "confirmed", _at(5)),
        ("y", _at(10), _at(40), "pending", None),
    ]
    assert _statuses(result) == {"x": "cancelled", "y": "invited"}


def test_same_minute_events_processed_in_input_order():
    # 10:10 先确认 x 再取消 x：均生效，席位释放后 y 同分钟补位
    result = _run(
        seats=1,
        settle=20,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("confirm", "x", 10), ("cancel", "x", 10)),
    )
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "confirmed", _at(10)),
        ("y", _at(10), _at(40), "pending", None),
    ]
    assert _statuses(result) == {"x": "cancelled", "y": "invited"}

    # 同刻反过来：先取消 x（y 补位），x 再确认已无有效邀请 -> 整批拒绝
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("cancel", "x", 10), ("confirm", "x", 10)),
        )
    assert exc_info.value.error == "no_valid_invitation"
    assert exc_info.value.reason == "already_exited"
    assert exc_info.value.event_index == 1


def test_cancel_then_confirm_refilled_candidate_same_minute():
    # 10:10 取消 x -> y 立即补位；同分钟随后的 y 确认生效
    result = _run(
        seats=1,
        settle=20,
        candidates=[_cand("x"), _cand("y")],
        events=_events(("cancel", "x", 10), ("confirm", "y", 10)),
    )
    assert _inv_rows(result)[1] == ("y", _at(10), _at(40), "confirmed", _at(10))
    assert _statuses(result) == {"x": "cancelled", "y": "confirmed"}


def test_multiple_seats_expire_same_minute_refill_in_queue_order():
    candidates = [_cand(c) for c in ("a", "b", "c", "d")]
    result = _run(seats=2, ttl=30, candidates=candidates)
    assert _inv_rows(result) == [
        ("a", _at(0), _at(30), "expired", _at(31)),
        ("b", _at(0), _at(30), "expired", _at(31)),
        ("c", _at(31), _at(61), "expired", _at(62)),
        ("d", _at(31), _at(61), "expired", _at(62)),
    ]
    assert all(s == "expired" for s in _statuses(result).values())


def test_cascading_expiries_until_waitlist_empty():
    result = _run(
        seats=1, ttl=10, settle=100, candidates=[_cand(c) for c in "abc"]
    )
    assert _inv_rows(result) == [
        ("a", _at(0), _at(10), "expired", _at(11)),
        ("b", _at(11), _at(21), "expired", _at(22)),
        ("c", _at(22), _at(32), "expired", _at(33)),
    ]


def test_pending_invitation_and_waiting_candidate_at_settlement():
    # 结算时刻早于自动失效分钟：邀请保持 pending，候补者保持待邀
    result = _run(
        seats=2, ttl=30, settle=20, candidates=[_cand(c) for c in "xyz"]
    )
    assert _inv_rows(result) == [
        ("x", _at(0), _at(30), "pending", None),
        ("y", _at(0), _at(30), "pending", None),
    ]
    assert _statuses(result) == {"x": "invited", "y": "invited", "z": "waiting"}


def test_expiry_after_settlement_not_processed():
    # 失效分钟 10:31 晚于结算 10:30：不处理
    result = _run(seats=1, ttl=30, settle=30, candidates=[_cand("x"), _cand("y")])
    assert result.invitations[0].result == "pending"
    assert _statuses(result) == {"x": "invited", "y": "waiting"}

    # 结算时刻等于失效分钟：处理
    result = _run(seats=1, ttl=30, settle=31, candidates=[_cand("x"), _cand("y")])
    assert result.invitations[0].result == "expired"
    assert _statuses(result)["x"] == "expired"


def test_event_at_settlement_minute_processed():
    result = _run(
        seats=1,
        settle=15,
        candidates=[_cand("x")],
        events=_events(("confirm", "x", 15)),
    )
    assert result.invitations[0].result == "confirmed"


def test_more_seats_than_candidates():
    result = _run(seats=5, ttl=30, settle=20, candidates=[_cand("a"), _cand("b")])
    assert [inv.candidate_id for inv in result.invitations] == ["a", "b"]
    assert _statuses(result) == {"a": "invited", "b": "invited"}


def test_registered_at_equal_start_allowed():
    result = _run(seats=1, candidates=[_cand("x", registered=0)])
    assert result.invitations[0].issued_at == T0


def test_no_events_defaults_to_empty():
    result = _run(seats=1, settle=20, candidates=[_cand("x")])
    assert len(result.invitations) == 1
    assert _statuses(result) == {"x": "invited"}


def test_confirm_unknown_candidate_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x")],
            events=_events(("confirm", "ghost", 5)),
        )
    exc = exc_info.value
    assert exc.error == "no_valid_invitation"
    assert exc.reason == "not_found"
    assert exc.candidate_id == "ghost"
    assert exc.detail() == {
        "error": "no_valid_invitation",
        "candidate_id": "ghost",
        "event_index": 0,
        "at_utc": "2026-04-01T10:05:00Z",
        "reason": "not_found",
    }


def test_confirm_waiting_candidate_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("confirm", "y", 5)),
        )
    assert exc_info.value.error == "no_valid_invitation"
    assert exc_info.value.reason == "not_invited"


def test_confirm_twice_rejected_as_already_confirmed():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x")],
            events=_events(("confirm", "x", 5), ("confirm", "x", 6)),
        )
    assert exc_info.value.error == "no_valid_invitation"
    assert exc_info.value.reason == "already_confirmed"
    assert exc_info.value.event_index == 1


def test_confirm_cancelled_candidate_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("cancel", "y", 5), ("confirm", "y", 6)),
        )
    assert exc_info.value.error == "no_valid_invitation"
    assert exc_info.value.reason == "already_exited"


def test_cancel_unknown_candidate_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x")],
            events=_events(("cancel", "ghost", 5)),
        )
    exc = exc_info.value
    assert exc.error == "cancel_target_invalid"
    assert exc.reason == "not_found"
    assert exc.detail()["reason"] == "not_found"


def test_cancel_already_cancelled_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("cancel", "y", 5), ("cancel", "y", 6)),
        )
    assert exc_info.value.error == "cancel_target_invalid"
    assert exc_info.value.reason == "already_exited"
    assert exc_info.value.event_index == 1


def test_cancel_already_expired_rejected():
    # x 于 10:31 自动失效；同刻取消 x -> 已退出，整批拒绝
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            ttl=30,
            candidates=[_cand("x"), _cand("y")],
            events=_events(("cancel", "x", 31)),
        )
    assert exc_info.value.error == "cancel_target_invalid"
    assert exc_info.value.reason == "already_exited"


def test_cancel_confirmed_then_confirm_rejected():
    with pytest.raises(WaitlistRejected) as exc_info:
        _run(
            seats=1,
            candidates=[_cand("x")],
            events=_events(
                ("confirm", "x", 5), ("cancel", "x", 6), ("confirm", "x", 7)
            ),
        )
    assert exc_info.value.error == "no_valid_invitation"
    assert exc_info.value.reason == "already_exited"


def test_candidate_invited_at_most_once():
    # x 失效退出后不会再次被邀请：席位由 y、z 依次递补
    result = _run(
        seats=1, ttl=10, settle=100, candidates=[_cand(c) for c in "xyz"]
    )
    assert [inv.candidate_id for inv in result.invitations] == ["x", "y", "z"]
    assert all(inv.result == "expired" for inv in result.invitations)


def test_outcomes_follow_input_order_not_queue_order():
    candidates = [_cand("b", priority=0), _cand("a", priority=9)]
    result = _run(seats=1, settle=20, candidates=candidates)
    # 邀请按候补顺序（a 优先），最终状态按输入顺序返回
    assert [inv.candidate_id for inv in result.invitations] == ["a"]
    assert [o.candidate_id for o in result.candidates] == ["b", "a"]
    assert _statuses(result) == {"a": "invited", "b": "waiting"}
