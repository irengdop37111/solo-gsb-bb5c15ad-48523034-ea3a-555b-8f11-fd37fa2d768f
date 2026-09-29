"""核心展开逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scheduler import (
    HandoffLeg,
    Instance,
    RestViolation,
    StaffingGap,
    classify,
    common_free_slots,
    expand_schedule,
    find_overlaps,
    handoff_route,
    intersect_intervals,
    interval_difference,
    merge_intervals,
    occupancy_by_day,
    rest_violations,
    schedule_changes,
    staffing_gaps,
)

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def test_classify_normal():
    kind, payload = classify(datetime(2026, 3, 7, 2, 30), NY)
    assert kind == "normal"
    assert payload[0] == datetime(2026, 3, 7, 7, 30, tzinfo=timezone.utc)


def test_classify_gap():
    # 2026-03-08 02:30 在纽约不存在（02:00 -> 03:00）
    kind, gap = classify(datetime(2026, 3, 8, 2, 30), NY)
    assert kind == "gap"
    assert gap == timedelta(hours=1)


def test_classify_fold():
    # 2026-11-01 01:30 在纽约出现两次
    kind, payload = classify(datetime(2026, 11, 1, 1, 30), NY)
    assert kind == "fold"
    first, second = payload
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)  # EDT
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)  # EST


def test_gap_skip():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={0, 1, 2, 3, 4, 5, 6},
        local_time=time(2, 30),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert instances == []
    assert skipped == [date(2026, 3, 8)]


def test_gap_shift_forward():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 EDT -> 07:30 UTC
    assert inst.utc == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert inst.utc.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)


def test_fold_strategies():
    base = dict(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
    )
    first, _ = expand_schedule(fold_strategy="first", **base)
    assert [i.reason for i in first] == ["fold_first"]
    assert first[0].utc == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    second, _ = expand_schedule(fold_strategy="second", **base)
    assert [i.reason for i in second] == ["fold_second"]
    assert second[0].utc == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    both, _ = expand_schedule(fold_strategy="both", **base)
    assert [i.reason for i in both] == ["fold_first", "fold_second"]
    assert both[0].utc < both[1].utc


def test_weekday_filter_and_utc_sort():
    # 2026-03-01 是周日；每周一、三 09:00，柏林时间
    instances, skipped = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 15),
        weekdays={0, 2},
        local_time=time(9, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 4  # 3/2, 3/4, 3/9, 3/11
    utcs = [i.utc for i in instances]
    assert utcs == sorted(utcs)
    assert len(set(utcs)) == len(utcs)  # 已去重
    assert all(i.reason == "normal" for i in instances)


def test_berlin_gap_shift():
    # 柏林 2026-03-29 02:30 不存在（02:00 -> 03:00）
    instances, skipped = expand_instances = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 29),
        end_date=date(2026, 3, 29),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="both",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 CEST -> 01:30 UTC
    assert inst.utc == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


def _inst(hh: int, mm: int = 0) -> Instance:
    return Instance(datetime(2026, 1, 1, hh, mm, tzinfo=timezone.utc), "normal")


def test_find_overlaps_basic():
    # A [08:00, 09:00)，B [08:30, 09:30) -> 重叠 [08:30, 09:00)
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=60,
    )
    assert ov.a_start == datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert ov.b_start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_boundary_touch_not_conflict():
    # 半开区间：A [08:00, 09:00) 与 B [09:00, 10:00) 边界相接，不算冲突
    assert (
        find_overlaps(
            a_instances=[_inst(8)],
            a_duration_minutes=60,
            b_instances=[_inst(9)],
            b_duration_minutes=60,
        )
        == []
    )


def test_find_overlaps_containment():
    # B 完全包含于 A
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=120,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=30,
    )
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_multiple_active_per_side():
    # A 侧两个并行区间都与同一 B 区间重叠，结果按重叠起点排序
    overlaps = find_overlaps(
        a_instances=[_inst(8), _inst(8, 30)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 15)],
        b_duration_minutes=60,
    )
    assert len(overlaps) == 2
    assert [o.start for o in overlaps] == [
        datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    ]
    assert all(o.minutes == 45 for o in overlaps)


def test_find_overlaps_empty_side():
    assert (
        find_overlaps(
            a_instances=[],
            a_duration_minutes=60,
            b_instances=[_inst(8)],
            b_duration_minutes=60,
        )
        == []
    )


# --- merge_intervals / occupancy_by_day ------------------------------------

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
        days=day - 1, hours=hh, minutes=mm
    )


def test_merge_intervals_overlapping_and_touching():
    merged = merge_intervals(
        [
            (_utc(1, 10), _utc(1, 11)),  # 与上一段相接 -> 并入
            (_utc(1, 8), _utc(1, 9)),
            (_utc(1, 8, 30), _utc(1, 10)),  # 与第一段相交 -> 并入
            (_utc(1, 12), _utc(1, 13)),  # 不相接，独立成段
        ]
    )
    assert merged == [(_utc(1, 8), _utc(1, 11)), (_utc(1, 12), _utc(1, 13))]


def test_merge_intervals_empty():
    assert merge_intervals([]) == []


def test_occupancy_by_day_basic():
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 8), "normal")],
        duration_minutes=90,
        report_tz=ZoneInfo("UTC"),
    )
    assert days == [(date(2026, 1, 1), 90.0)]


def test_occupancy_by_day_cross_midnight_split():
    # 23:00 UTC + 120min -> 当日 60 分钟 + 次日 60 分钟
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 23), "normal")],
        duration_minutes=120,
        report_tz=ZoneInfo("UTC"),
    )
    assert days == [(date(2026, 1, 1), 60.0), (date(2026, 1, 2), 60.0)]


def test_occupancy_by_day_report_timezone_shifts_date():
    # 2026-01-01 23:30 UTC + 60min，在上海（UTC+8）是 01-02 07:30-08:30
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 23, 30), "normal")],
        duration_minutes=60,
        report_tz=SHANGHAI,
    )
    assert days == [(date(2026, 1, 2), 60.0)]


def test_occupancy_by_day_empty_instances():
    assert (
        occupancy_by_day(instances=[], duration_minutes=60, report_tz=ZoneInfo("UTC"))
        == []
    )


def test_occupancy_by_day_fold_both_counts_overlap_once():
    # 纽约 2026-11-01 01:30 出现两次：05:30 / 06:30 UTC；各 90 分钟
    # -> [05:30,07:00) 与 [06:30,08:00) 重叠，合并后 [05:30,08:00) = 150 分钟
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
        fold_strategy="both",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=90, report_tz=NY
    )
    assert days == [(date(2026, 11, 1), 150.0)]


def test_occupancy_by_day_spring_forward_real_minutes():
    # 纽约 2026-03-07 23:00 EST = 03-08 04:00 UTC，180 分钟
    # -> 03-08 03:00 EDT（春跳当日仅 23 小时，仍按真实 UTC 时长累计）
    # 本地午夜 03-08 00:00 EST = 05:00 UTC：03-07 得 60 分钟，03-08 得 120 分钟
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 7),
        end_date=date(2026, 3, 7),
        weekdays={5},
        local_time=time(23, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=180, report_tz=NY
    )
    assert days == [(date(2026, 3, 7), 60.0), (date(2026, 3, 8), 120.0)]


def test_occupancy_by_day_fall_back_day_real_minutes():
    # 纽约 2026-11-01 00:30 EDT = 04:30 UTC，120 分钟 -> 02:30 EST
    # 跨越秋回（当日 25 小时），仍按真实 120 分钟计入 11-01
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(0, 30),
        gap_strategy="skip",
        fold_strategy="first",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=120, report_tz=NY
    )
    assert days == [(date(2026, 11, 1), 120.0)]


# --- staffing_gaps ----------------------------------------------------------


def test_staffing_gaps_basic_sweep():
    # alice [08:00,09:00)，bob [08:30,09:30)；min_staff=2
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=2,
        employee_intervals={
            "alice": [(_utc(1, 8), _utc(1, 9))],
            "bob": [(_utc(1, 8, 30), _utc(1, 9, 30))],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 0), _utc(1, 8), (), 2),
        StaffingGap(_utc(1, 8), _utc(1, 8, 30), ("alice",), 1),
        StaffingGap(_utc(1, 9), _utc(1, 9, 30), ("bob",), 1),
        StaffingGap(_utc(1, 9, 30), _utc(2, 0), (), 2),
    ]


def test_staffing_gaps_clips_to_window_and_merges_per_employee():
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=1,
        employee_intervals={
            "alice": [
                (_utc(0, 23), _utc(1, 1)),  # 裁剪到 [00:00, 01:00)
                (_utc(1, 8), _utc(1, 9)),
                (_utc(1, 8, 30), _utc(1, 10)),  # 与上一条相交 -> 合并
                (_utc(1, 10), _utc(1, 11)),  # 与合并段相接 -> 并入
            ],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 1), _utc(1, 8), (), 1),
        StaffingGap(_utc(1, 11), _utc(2, 0), (), 1),
    ]


def test_staffing_gaps_same_instant_events_processed_together():
    # alice 09:00 下岗与 bob 09:00 上岗同一时刻一起处理，不产生零长缺口
    assert (
        staffing_gaps(
            window_start=_utc(1, 8),
            window_end=_utc(1, 10),
            min_staff=1,
            employee_intervals={
                "alice": [(_utc(1, 8), _utc(1, 9))],
                "bob": [(_utc(1, 9), _utc(1, 10))],
            },
        )
        == []
    )


def test_staffing_gaps_on_duty_sorted_and_missing():
    gaps = staffing_gaps(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        min_staff=3,
        employee_intervals={
            "zoe": [(_utc(1, 8), _utc(1, 10))],
            "amy": [(_utc(1, 9), _utc(1, 10))],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 8), _utc(1, 9), ("zoe",), 2),
        StaffingGap(_utc(1, 9), _utc(1, 10), ("amy", "zoe"), 1),
    ]


def test_staffing_gaps_empty_when_staffed():
    # 单人在岗区间覆盖整个窗口（含越界部分被裁剪）
    assert (
        staffing_gaps(
            window_start=_utc(1, 0),
            window_end=_utc(2, 0),
            min_staff=1,
            employee_intervals={"alice": [(_utc(0, 12), _utc(2, 12))]},
        )
        == []
    )


def test_staffing_gaps_ignores_intervals_outside_window():
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=1,
        employee_intervals={"alice": [(_utc(3, 0), _utc(3, 1))]},
    )
    assert gaps == [StaffingGap(_utc(1, 0), _utc(2, 0), (), 1)]


# --- rest_violations --------------------------------------------------------


def test_rest_violations_basic():
    # 工作段 [08:00,09:00) 与 [10:00,11:00)，空档 60 分钟 < 阈值 90
    violations = rest_violations(
        intervals=[(_utc(1, 8), _utc(1, 9)), (_utc(1, 10), _utc(1, 11))],
        min_rest_minutes=90,
    )
    assert violations == [RestViolation(_utc(1, 9), _utc(1, 10))]
    assert violations[0].rest_minutes == 60


def test_rest_violations_touching_intervals_merged_no_violation():
    # 相接区间合并为连续工作 [08:00,10:00)，不产生空档（无零/负休息）
    assert (
        rest_violations(
            intervals=[(_utc(1, 8), _utc(1, 9)), (_utc(1, 9), _utc(1, 10))],
            min_rest_minutes=90,
        )
        == []
    )
    # 相交区间同样合并
    assert (
        rest_violations(
            intervals=[(_utc(1, 8), _utc(1, 9, 30)), (_utc(1, 9), _utc(1, 10))],
            min_rest_minutes=1,
        )
        == []
    )


def test_rest_violations_exact_threshold_not_violation():
    # 空档恰好等于阈值，不算低于阈值
    assert (
        rest_violations(
            intervals=[(_utc(1, 8), _utc(1, 9)), (_utc(1, 10), _utc(1, 11))],
            min_rest_minutes=60,
        )
        == []
    )


def test_rest_violations_sorted_multiple():
    # 三段工作产生两个违规空档，按起点排序
    violations = rest_violations(
        intervals=[
            (_utc(1, 8), _utc(1, 9)),
            (_utc(1, 9, 30), _utc(1, 10)),
            (_utc(1, 10, 45), _utc(1, 12)),
        ],
        min_rest_minutes=60,
    )
    assert violations == [
        RestViolation(_utc(1, 9), _utc(1, 9, 30)),
        RestViolation(_utc(1, 10), _utc(1, 10, 45)),
    ]
    assert [v.rest_minutes for v in violations] == [30, 45]


def test_rest_violations_unsorted_input_and_single_segment():
    # 输入无需有序；不足两段工作无空档可比
    violations = rest_violations(
        intervals=[(_utc(1, 10), _utc(1, 11)), (_utc(1, 8), _utc(1, 9))],
        min_rest_minutes=200,
    )
    assert violations == [RestViolation(_utc(1, 9), _utc(1, 10))]
    assert rest_violations(intervals=[(_utc(1, 8), _utc(1, 9))], min_rest_minutes=1) == []
    assert rest_violations(intervals=[], min_rest_minutes=1) == []


# --- common_free_slots ------------------------------------------------------


def test_common_free_slots_basic():
    # alice 忙 [08:00,09:00)，bob 忙 [09:30,10:30)；窗口 [08:00,12:00)
    # 共同空闲 [09:00,09:30) 与 [10:30,12:00)；时长 30、步长 15
    slots = common_free_slots(
        window_start=_utc(1, 8),
        window_end=_utc(1, 12),
        duration_minutes=30,
        step_minutes=15,
        participant_intervals=[
            [(_utc(1, 8), _utc(1, 9))],
            [(_utc(1, 9, 30), _utc(1, 10, 30))],
        ],
    )
    assert slots == [
        (_utc(1, 9), _utc(1, 9, 30)),  # 两端均与忙碌相接，允许
        (_utc(1, 10, 30), _utc(1, 11)),
        (_utc(1, 10, 45), _utc(1, 11, 15)),
        (_utc(1, 11), _utc(1, 11, 30)),
        (_utc(1, 11, 15), _utc(1, 11, 45)),
        (_utc(1, 11, 30), _utc(1, 12)),
    ]


def test_common_free_slots_grid_origin_is_window_start():
    # 网格以窗口起点为原点，而非整点：08:07 起每 20 分钟一格
    slots = common_free_slots(
        window_start=_utc(1, 8, 7),
        window_end=_utc(1, 9, 7),
        duration_minutes=20,
        step_minutes=20,
        participant_intervals=[[]],
    )
    assert slots == [
        (_utc(1, 8, 7), _utc(1, 8, 27)),
        (_utc(1, 8, 27), _utc(1, 8, 47)),
        (_utc(1, 8, 47), _utc(1, 9, 7)),
    ]


def test_common_free_slots_merges_busy_and_counts_duplicate_once():
    # 同一参与者相交区间合并；完全重复的实例不重复占用
    slots = common_free_slots(
        window_start=_utc(1, 8),
        window_end=_utc(1, 12),
        duration_minutes=60,
        step_minutes=30,
        participant_intervals=[
            [(_utc(1, 8), _utc(1, 9)), (_utc(1, 8, 30), _utc(1, 10))],  # 相交 -> [08:00,10:00)
            [(_utc(1, 10), _utc(1, 11)), (_utc(1, 10), _utc(1, 11))],  # 重复 -> 只计一次
        ],
    )
    # 忙碌并集 [08:00,11:00)，共同空闲仅 [11:00,12:00)
    assert slots == [(_utc(1, 11), _utc(1, 12))]


def test_common_free_slots_clips_to_window():
    # 忙碌区间越界部分被裁剪；网格点早于空闲段起点时被跳过
    slots = common_free_slots(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        duration_minutes=60,
        step_minutes=60,
        participant_intervals=[[(_utc(0, 23), _utc(1, 8, 30))]],
    )
    # 忙碌裁剪为 [08:00,08:30)；网格点 08:00 放不下，仅 09:00 可行
    assert slots == [(_utc(1, 9), _utc(1, 10))]


def test_common_free_slots_empty_when_no_segment_fits():
    # 唯一空闲段 [08:15,09:00) 仅 45 分钟，容纳不下 60 分钟预约
    assert (
        common_free_slots(
            window_start=_utc(1, 8),
            window_end=_utc(1, 9),
            duration_minutes=60,
            step_minutes=15,
            participant_intervals=[[(_utc(1, 8), _utc(1, 8, 15))]],
        )
        == []
    )


def test_common_free_slots_all_participants_must_be_free():
    # 三人轮转占满全天：任何时刻都有人忙 -> 无共同空闲
    slots = common_free_slots(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        duration_minutes=30,
        step_minutes=30,
        participant_intervals=[
            [(_utc(1, 0), _utc(1, 8))],
            [(_utc(1, 8), _utc(1, 16))],
            [(_utc(1, 16), _utc(2, 0))],
        ],
    )
    assert slots == []


# --- interval_difference / schedule_changes ---------------------------------


def test_interval_difference_basic():
    # base [08:00,12:00) 挖去 [09:00,10:00) -> 两段最大连续区间
    assert interval_difference(
        [(_utc(1, 8), _utc(1, 12))],
        [(_utc(1, 9), _utc(1, 10))],
    ) == [(_utc(1, 8), _utc(1, 9)), (_utc(1, 10), _utc(1, 12))]


def test_interval_difference_merges_and_splits_unsorted_input():
    # 两侧输入均无需有序；base 内相交/相接段先合并，subtract 同样先合并
    assert interval_difference(
        [(_utc(1, 10), _utc(1, 12)), (_utc(1, 8), _utc(1, 10))],  # 相接 -> [08:00,12:00)
        [(_utc(1, 9), _utc(1, 9, 30)), (_utc(1, 9, 30), _utc(1, 10))],  # -> [09:00,10:00)
    ) == [(_utc(1, 8), _utc(1, 9)), (_utc(1, 10), _utc(1, 12))]


def test_interval_difference_touching_subtract_keeps_base_whole():
    # 半开区间：subtract 与 base 边界相接不挖去任何部分
    assert interval_difference(
        [(_utc(1, 8), _utc(1, 10))],
        [(_utc(1, 10), _utc(1, 11)), (_utc(1, 7), _utc(1, 8))],
    ) == [(_utc(1, 8), _utc(1, 10))]


def test_interval_difference_containment_and_cover():
    # subtract 完全包含 base -> 空；subtract 被 base 包含 -> 两段
    assert (
        interval_difference(
            [(_utc(1, 8), _utc(1, 9))],
            [(_utc(1, 7), _utc(1, 10))],
        )
        == []
    )
    assert interval_difference([], [(_utc(1, 8), _utc(1, 9))]) == []
    assert interval_difference([(_utc(1, 8), _utc(1, 9))], []) == [
        (_utc(1, 8), _utc(1, 9))
    ]


def test_schedule_changes_clips_to_window_and_compares():
    # 窗口 [08:00, 20:00)：变更前 [07:00,10:00)（越界裁剪），变更后 [09:00,11:00)
    cancelled, added = schedule_changes(
        window_start=_utc(1, 8),
        window_end=_utc(1, 20),
        before_intervals=[(_utc(1, 7), _utc(1, 10))],
        after_intervals=[(_utc(1, 9), _utc(1, 11))],
    )
    assert cancelled == [(_utc(1, 8), _utc(1, 9))]
    assert added == [(_utc(1, 10), _utc(1, 11))]


def test_schedule_changes_same_coverage_empty_diff():
    # 仅实例条目变化：一段 120 分钟 vs 两段相接 60 分钟 -> 覆盖相同，无差异
    cancelled, added = schedule_changes(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        before_intervals=[(_utc(1, 8), _utc(1, 10))],
        after_intervals=[(_utc(1, 8), _utc(1, 9)), (_utc(1, 9), _utc(1, 10))],
    )
    assert cancelled == []
    assert added == []


def test_schedule_changes_empty_sides():
    # 变更前为空 -> 全部为新增；变更后为空 -> 全部为取消；均空 -> 无差异
    cancelled, added = schedule_changes(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        before_intervals=[],
        after_intervals=[(_utc(1, 8), _utc(1, 9))],
    )
    assert cancelled == []
    assert added == [(_utc(1, 8), _utc(1, 9))]

    cancelled, added = schedule_changes(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        before_intervals=[(_utc(1, 8), _utc(1, 9))],
        after_intervals=[],
    )
    assert cancelled == [(_utc(1, 8), _utc(1, 9))]
    assert added == []

    assert schedule_changes(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        before_intervals=[],
        after_intervals=[],
    ) == ([], [])


# --- handoff_route / intersect_intervals -------------------------------------


def test_intersect_intervals_basic_and_touch():
    # [08,10) 与 [09,11) 交 [09,10)；相接（终点 == 起点）交集为空
    assert intersect_intervals(
        [(_utc(1, 8), _utc(1, 10))], [(_utc(1, 9), _utc(1, 11))]
    ) == [(_utc(1, 9), _utc(1, 10))]
    assert (
        intersect_intervals(
            [(_utc(1, 8), _utc(1, 9))], [(_utc(1, 9), _utc(1, 10))]
        )
        == []
    )
    # 多段：[8,10)+[14,16) 与 [9,15) 交 [9,10)+[14,15)
    assert intersect_intervals(
        [(_utc(1, 8), _utc(1, 10)), (_utc(1, 14), _utc(1, 16))],
        [(_utc(1, 9), _utc(1, 15))],
    ) == [(_utc(1, 9), _utc(1, 10)), (_utc(1, 14), _utc(1, 15))]


def _handoff(**overrides):
    kwargs = dict(
        message_at=_utc(1, 0),
        deadline=_utc(2, 0),
        holder="a",
        target="c",
        duration_minutes=30,
        employee_intervals={},
    )
    kwargs.update(overrides)
    return handoff_route(**kwargs)


def test_handoff_holder_is_target_returns_message_time_empty_chain():
    arrival, chain = _handoff(target="a")
    assert arrival == _utc(1, 0)
    assert chain == []


def test_handoff_direct_earliest_overlap():
    # a 在岗 [08,12)，c 在岗 [09,11)：共同段 [09,11)，交接 30 分钟 -> [09,09:30)
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 12))],
            "c": [(_utc(1, 9), _utc(1, 11))],
        }
    )
    assert arrival == _utc(1, 9, 30)
    assert chain == [HandoffLeg("a", "c", _utc(1, 9), _utc(1, 9, 30))]


def test_handoff_touching_shifts_merge_before_overlap():
    # a 两段相接 [08,09)+[09,10) 合并为 [08,10)；c [09:30,11)：
    # 合并后才能在连续共同在岗 [09:30,10) 内完成 30 分钟交接
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 9)), (_utc(1, 9), _utc(1, 10))],
            "c": [(_utc(1, 9, 30), _utc(1, 11))],
        }
    )
    assert arrival == _utc(1, 10)
    assert chain == [HandoffLeg("a", "c", _utc(1, 9, 30), _utc(1, 10))]


def test_handoff_message_ready_caps_start():
    # 消息 09:15 才产生（a 自 08 起在岗）；共同段 [08,12)，交接自 09:15 起
    arrival, chain = _handoff(
        message_at=_utc(1, 9, 15),
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 12))],
            "c": [(_utc(1, 8), _utc(1, 12))],
        },
    )
    assert arrival == _utc(1, 9, 45)
    assert chain == [HandoffLeg("a", "c", _utc(1, 9, 15), _utc(1, 9, 45))]


def test_handoff_holder_keeps_message_off_duty_and_waits():
    # a [08,09) 与 c [08:40,09:30) 共同段仅 20 分钟，不足 30；a 下一班
    # [12,13) 与 c [12,13) 完整共同在岗 -> 离岗期间保留消息，12:30 完成交接
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 9)), (_utc(1, 12), _utc(1, 13))],
            "c": [(_utc(1, 8, 40), _utc(1, 9, 30)), (_utc(1, 12), _utc(1, 13))],
        }
    )
    assert arrival == _utc(1, 12, 30)
    assert chain == [HandoffLeg("a", "c", _utc(1, 12), _utc(1, 12, 30))]


def test_handoff_multihop_chain():
    # a 与 c 从不共同在岗，经 b 中转：
    # a[08,10) 与 b[08:30,09:30) -> b 09:00 持有；b[11,12) 与 c[11,13) -> c 11:30
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 10))],
            "b": [(_utc(1, 8, 30), _utc(1, 9, 30)), (_utc(1, 11), _utc(1, 12))],
            "c": [(_utc(1, 11), _utc(1, 13))],
        }
    )
    assert arrival == _utc(1, 11, 30)
    assert chain == [
        HandoffLeg("a", "b", _utc(1, 8, 30), _utc(1, 9)),
        HandoffLeg("b", "c", _utc(1, 11), _utc(1, 11, 30)),
    ]


def test_handoff_earliest_route_beats_direct_later():
    # 直交 a->c 共同在岗 13:15 才出现（13:45 到达）；经 b 中转 11:30 即可到达
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 9)), (_utc(1, 13), _utc(1, 14))],
            "b": [(_utc(1, 8, 30), _utc(1, 9, 30)), (_utc(1, 11), _utc(1, 12))],
            "c": [(_utc(1, 11), _utc(1, 13)), (_utc(1, 13, 15), _utc(1, 14, 15))],
        }
    )
    assert arrival == _utc(1, 11, 30)
    assert [leg.receiver for leg in chain] == ["b", "c"]
    assert chain[0] == HandoffLeg("a", "b", _utc(1, 8, 30), _utc(1, 9))
    assert chain[1] == HandoffLeg("b", "c", _utc(1, 11), _utc(1, 11, 30))


def test_handoff_no_overlap_returns_null():
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 9))],
            "c": [(_utc(1, 20), _utc(1, 21))],
        }
    )
    assert arrival is None
    assert chain == []


def test_handoff_joint_segment_too_short_returns_null():
    # 共同在岗仅 20 分钟，交接需 30 分钟 -> 无解
    arrival, chain = _handoff(
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 9))],
            "c": [(_utc(1, 8, 40), _utc(1, 9))],
        }
    )
    assert arrival is None
    assert chain == []


def test_handoff_finish_after_deadline_returns_null():
    # 共同段 [08:30, 09:00) 可完成交接，但截止 08:45 -> 终点超截止，无解
    arrival, chain = _handoff(
        deadline=_utc(1, 8, 45),
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 10))],
            "c": [(_utc(1, 8, 30), _utc(1, 10))],
        },
    )
    assert arrival is None
    assert chain == []


def test_handoff_finish_exactly_at_deadline_ok():
    # 终点恰好等于截止时刻（不晚于截止）-> 有解
    arrival, chain = _handoff(
        deadline=_utc(1, 9),
        employee_intervals={
            "a": [(_utc(1, 8), _utc(1, 10))],
            "c": [(_utc(1, 8, 30), _utc(1, 10))],
        },
    )
    assert arrival == _utc(1, 9)
    assert chain == [HandoffLeg("a", "c", _utc(1, 8, 30), _utc(1, 9))]


def test_handoff_unreachable_target_with_empty_schedule():
    arrival, chain = _handoff(
        employee_intervals={"a": [(_utc(1, 8), _utc(1, 10))], "c": []}
    )
    assert arrival is None
    assert chain == []
