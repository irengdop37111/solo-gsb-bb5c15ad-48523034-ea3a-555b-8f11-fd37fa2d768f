"""API 层测试：正常展开与 422 校验。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = {
    "timezone": "America/New_York",
    "start_date": "2026-03-08",
    "end_date": "2026-03-08",
    "weekdays": [6],
    "local_time": "02:30",
    "gap_strategy": "shift_forward",
    "fold_strategy": "both",
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_expand_gap_shift_forward():
    resp = client.post("/expand", json=BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["skipped_count"] == 0
    inst = body["instances"][0]
    assert inst["local"] == "2026-03-08T03:30:00-04:00"
    assert inst["offset"] == "-04:00"
    assert inst["utc"] == "2026-03-08T07:30:00Z"
    assert inst["reason"] == "gap_shifted_forward"


def test_expand_gap_skip():
    resp = client.post("/expand", json={**BASE, "gap_strategy": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped_dates"] == ["2026-03-08"]


def test_expand_fold_both():
    payload = {
        **BASE,
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "local_time": "01:30",
    }
    resp = client.post("/expand", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    first, second = body["instances"]
    assert first["reason"] == "fold_first"
    assert first["offset"] == "-04:00"
    assert first["utc"] == "2026-11-01T05:30:00Z"
    assert second["reason"] == "fold_second"
    assert second["offset"] == "-05:00"
    assert second["utc"] == "2026-11-01T06:30:00Z"
    # 本地时钟面相同，但为两个不同的 UTC 实例
    assert first["local"] == "2026-11-01T01:30:00-04:00"
    assert second["local"] == "2026-11-01T01:30:00-05:00"


def test_invalid_timezone_422():
    resp = client.post("/expand", json={**BASE, "timezone": "Not/AZone"})
    assert resp.status_code == 422


def test_reversed_range_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-04-01", "end_date": "2026-03-01"},
    )
    assert resp.status_code == 422


def test_range_over_366_days_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-01-01", "end_date": "2027-01-03"},
    )
    assert resp.status_code == 422


def test_range_exactly_366_days_ok():
    resp = client.post(
        "/expand",
        json={
            **BASE,
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",  # 相差 366 天
            "local_time": "09:00",
        },
    )
    assert resp.status_code == 200


def test_invalid_weekday_422():
    resp = client.post("/expand", json={**BASE, "weekdays": [7]})
    assert resp.status_code == 422


def test_empty_weekdays_422():
    resp = client.post("/expand", json={**BASE, "weekdays": []})
    assert resp.status_code == 422


# --- POST /overlaps ---------------------------------------------------------

# A 侧：柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，持续 60 分钟
OVERLAP_A = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
}
# B 侧：纽约同日 03:30 EST = 08:30 UTC，持续 60 分钟
OVERLAP_B = {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60,
}


def test_overlaps_basic():
    resp = client.post("/overlaps", json={"a": OVERLAP_A, "b": OVERLAP_B})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["a_start_utc"] == "2026-03-02T08:00:00Z"
    assert ov["b_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-02T09:00:00Z"
    assert ov["minutes"] == 30


def test_overlaps_boundary_touch_not_conflict():
    # B 改为 04:00 EST = 09:00 UTC，与 A 的终点 09:00 UTC 恰好相接
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "local_time": "04:00"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_sorted_multiple():
    # 两侧都扩到周一、周二；A 09:00+120min，B 04:30 EST+60min = 09:30 UTC
    a = {
        **OVERLAP_A,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "duration_minutes": 120,
    }
    b = {
        **OVERLAP_B,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "local_time": "04:30",
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    starts = [ov["overlap_start_utc"] for ov in body["overlaps"]]
    assert starts == sorted(starts)
    assert starts == ["2026-03-02T09:30:00Z", "2026-03-03T09:30:00Z"]
    assert all(ov["overlap_end_utc"].endswith("T10:00:00Z") for ov in body["overlaps"])
    assert all(ov["minutes"] == 30 for ov in body["overlaps"])


def test_overlaps_fold_both_counts_first_occurrence():
    # 纽约 2026-11-01 01:30 出现两次：05:30 UTC / 06:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "05:45",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "01:30",
        "fold_strategy": "both",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    # 仅第一次出现 [05:30, 06:30) 与 A [05:45, 06:15) 重叠
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-11-01T05:30:00Z"
    assert ov["overlap_start_utc"] == "2026-11-01T05:45:00Z"
    assert ov["overlap_end_utc"] == "2026-11-01T06:15:00Z"
    assert ov["minutes"] == 30

    # 只取第二次出现时不再重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "fold_strategy": "second"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_gap_strategies():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "07:30",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "02:30",
        "gap_strategy": "shift_forward",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-08T08:00:00Z"
    assert ov["minutes"] == 30

    # skip 策略下该次被跳过，无重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "gap_strategy": "skip"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_duration_zero_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "duration_minutes": 0}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422


def test_overlaps_duration_over_1440_422():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1441}},
    )
    assert resp.status_code == 422


def test_overlaps_duration_1440_ok():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1440}},
    )
    assert resp.status_code == 200


def test_overlaps_side_range_over_366_days_422():
    resp = client.post(
        "/overlaps",
        json={
            "a": OVERLAP_A,
            "b": {**OVERLAP_B, "start_date": "2026-01-01", "end_date": "2027-01-03"},
        },
    )
    assert resp.status_code == 422


def test_overlaps_invalid_timezone_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "timezone": "Not/AZone"}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422


# --- POST /daily-occupancy --------------------------------------------------

# 柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，持续 60 分钟
OCCUPANCY_BASE = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
    "report_timezone": "Europe/Berlin",
}


def test_daily_occupancy_basic():
    resp = client.post("/daily-occupancy", json=OCCUPANCY_BASE)
    assert resp.status_code == 200
    assert resp.json() == {
        "report_timezone": "Europe/Berlin",
        "count": 1,
        "days": [{"date": "2026-03-02", "minutes": 60.0}],
    }


def test_daily_occupancy_cross_midnight_split_sorted():
    # UTC 23:00 + 120min，跨两个本地日；结果按日期升序
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "UTC",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "weekdays": [3, 4],
            "local_time": "23:00",
            "duration_minutes": 120,
            "report_timezone": "UTC",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert body["days"] == [
        {"date": "2026-01-01", "minutes": 60.0},
        {"date": "2026-01-02", "minutes": 120.0},
        {"date": "2026-01-03", "minutes": 60.0},
    ]


def test_daily_occupancy_report_timezone_shifts_date():
    # 柏林 09:00 CET = 08:00 UTC，在上海（UTC+8）是当日 16:00；
    # 而 UTC 23:30 的实例在上海落入次日
    resp = client.post(
        "/daily-occupancy",
        json={**OCCUPANCY_BASE, "report_timezone": "Asia/Shanghai"},
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-03-02", "minutes": 60.0}]

    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "UTC",
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "weekdays": [3],
            "local_time": "23:30",
            "duration_minutes": 60,
            "report_timezone": "Asia/Shanghai",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-01-02", "minutes": 60.0}]


def test_daily_occupancy_spring_forward_real_minutes():
    # 纽约 2026-03-07 23:00 EST + 180min，跨春跳（当日 23 小时）：
    # 03-07 得 60 分钟，03-08 得 120 分钟，按真实 UTC 时长累计
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-03-07",
            "end_date": "2026-03-07",
            "weekdays": [5],
            "local_time": "23:00",
            "duration_minutes": 180,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [
        {"date": "2026-03-07", "minutes": 60.0},
        {"date": "2026-03-08", "minutes": 120.0},
    ]


def test_daily_occupancy_fold_both_counts_overlap_once():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 90 分钟：
    # 重叠区间只计一次 -> 合并为 [05:30, 08:00) = 150 分钟
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-11-01",
            "end_date": "2026-11-01",
            "weekdays": [6],
            "local_time": "01:30",
            "fold_strategy": "both",
            "duration_minutes": 90,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-11-01", "minutes": 150.0}]


def test_daily_occupancy_no_instances_empty_list():
    # 纽约 2026-03-08 02:30 不存在，skip 策略下无实例 -> 空列表
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-03-08",
            "end_date": "2026-03-08",
            "weekdays": [6],
            "local_time": "02:30",
            "gap_strategy": "skip",
            "duration_minutes": 60,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "report_timezone": "America/New_York",
        "count": 0,
        "days": [],
    }


def test_daily_occupancy_invalid_report_timezone_422():
    resp = client.post(
        "/daily-occupancy",
        json={**OCCUPANCY_BASE, "report_timezone": "Not/AZone"},
    )
    assert resp.status_code == 422


def test_daily_occupancy_reuses_schedule_validation():
    # 复用现有校验：非法时区 / 逆序区间 / 超 366 天 / 时长越界均 422
    resp = client.post(
        "/daily-occupancy", json={**OCCUPANCY_BASE, "timezone": "Not/AZone"}
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy",
        json={
            **OCCUPANCY_BASE,
            "start_date": "2026-04-01",
            "end_date": "2026-03-01",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy",
        json={
            **OCCUPANCY_BASE,
            "start_date": "2026-01-01",
            "end_date": "2027-01-03",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy", json={**OCCUPANCY_BASE, "duration_minutes": 0}
    )
    assert resp.status_code == 422


# --- POST /staffing-gaps ----------------------------------------------------

# 窗口：2026-03-02 全天（UTC）
STAFF_WINDOW = {
    "window_start_utc": "2026-03-02T00:00:00Z",
    "window_end_utc": "2026-03-03T00:00:00Z",
}
# 柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，60 分钟
STAFF_SCHED_BERLIN = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
}
# 纽约同日 03:30 EST = 08:30 UTC，60 分钟
STAFF_SCHED_NY = {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60,
}


def _staff_payload(**overrides):
    payload = {
        **STAFF_WINDOW,
        "min_staff": 2,
        "employees": [
            {"id": "alice", "schedules": [STAFF_SCHED_BERLIN]},
            {"id": "bob", "schedules": [STAFF_SCHED_NY]},
        ],
    }
    payload.update(overrides)
    return payload


def _utc_schedule(local_time: str, duration: int = 60) -> dict:
    return {
        "timezone": "UTC",
        "start_date": "2026-03-02",
        "end_date": "2026-03-02",
        "weekdays": [0],
        "local_time": local_time,
        "duration_minutes": duration,
    }


def test_staffing_gaps_basic():
    resp = client.post("/staffing-gaps", json=_staff_payload())
    assert resp.status_code == 200
    body = resp.json()
    # alice [08:00,09:00)，bob [08:30,09:30)；min_staff=2
    assert body["count"] == 4
    assert body["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 2,
        },
        {
            "start_utc": "2026-03-02T08:00:00Z",
            "end_utc": "2026-03-02T08:30:00Z",
            "on_duty": ["alice"],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T09:00:00Z",
            "end_utc": "2026-03-02T09:30:00Z",
            "on_duty": ["bob"],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T09:30:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 2,
        },
    ]


def test_staffing_gaps_fully_staffed_returns_empty():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("00:00", 1440)]}
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "gaps": []}


def test_staffing_gaps_null_schedules_never_on_duty():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[{"id": "alice", "schedules": None}, {"id": "bob"}],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "count": 1,
        "gaps": [
            {
                "start_utc": "2026-03-02T00:00:00Z",
                "end_utc": "2026-03-03T00:00:00Z",
                "on_duty": [],
                "missing": 1,
            }
        ],
    }


def test_staffing_gaps_clips_to_window():
    # 实例 2026-03-01 23:00 UTC 起 120 分钟，仅 [00:00, 01:00) 落在窗口内
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "UTC",
                            "start_date": "2026-03-01",
                            "end_date": "2026-03-01",
                            "weekdays": [6],
                            "local_time": "23:00",
                            "duration_minutes": 120,
                        }
                    ],
                }
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T01:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        }
    ]


def test_staffing_gaps_same_employee_touching_intervals_count_once():
    # alice 两段相接日程 08:00+60 / 09:00+60 -> 连续在岗 [08:00,10:00)，只计一人
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        _utc_schedule("08:00"),
                        _utc_schedule("09:00"),
                    ],
                }
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T10:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_same_instant_handoff_no_zero_gap():
    # alice [08:00,09:00)，bob [09:00,10:00)：09:00 的上下岗一起处理
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00")]},
                {"id": "bob", "schedules": [_utc_schedule("09:00")]},
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T10:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_on_duty_ids_sorted():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=3,
            employees=[
                {"id": "zoe", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "amy", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "bob", "schedules": None},
            ],
        ),
    )
    assert resp.status_code == 200
    gaps = resp.json()["gaps"]
    assert gaps[1] == {
        "start_utc": "2026-03-02T08:00:00Z",
        "end_utc": "2026-03-02T10:00:00Z",
        "on_duty": ["amy", "zoe"],
        "missing": 1,
    }


def test_staffing_gaps_reuses_fold_expansion():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 60 分钟且相接
    # -> 同一员工合并为连续在岗 [05:30, 07:30)，只计一人
    resp = client.post(
        "/staffing-gaps",
        json={
            "window_start_utc": "2026-11-01T00:00:00Z",
            "window_end_utc": "2026-11-02T00:00:00Z",
            "min_staff": 1,
            "employees": [
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-11-01",
                            "end_date": "2026-11-01",
                            "weekdays": [6],
                            "local_time": "01:30",
                            "fold_strategy": "both",
                            "duration_minutes": 60,
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-11-01T00:00:00Z",
            "end_utc": "2026-11-01T05:30:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-11-01T07:30:00Z",
            "end_utc": "2026-11-02T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_reuses_gap_shift_forward():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    resp = client.post(
        "/staffing-gaps",
        json={
            "window_start_utc": "2026-03-08T00:00:00Z",
            "window_end_utc": "2026-03-09T00:00:00Z",
            "min_staff": 1,
            "employees": [
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-03-08",
                            "end_date": "2026-03-08",
                            "weekdays": [6],
                            "local_time": "02:30",
                            "gap_strategy": "shift_forward",
                            "duration_minutes": 60,
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-08T00:00:00Z",
            "end_utc": "2026-03-08T07:30:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-08T08:30:00Z",
            "end_utc": "2026-03-09T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_window_must_be_z_utc_422():
    # 缺少 Z 后缀 / 用 +00:00 代替 Z 均非法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_start_utc="2026-03-02T00:00:00"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_start_utc="2026-03-02T00:00:00+00:00"),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_not_increasing_422():
    # 相等与逆序都非法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_end_utc="2026-03-02T00:00:00Z"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-03-03T00:00:00Z",
            window_end_utc="2026-03-02T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_over_366_days_422():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2027-01-03T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_exactly_366_days_ok():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2027-01-02T00:00:00Z",  # 恰好 366 天
        ),
    )
    assert resp.status_code == 200


def test_staffing_gaps_min_staff_bounds_422():
    resp = client.post("/staffing-gaps", json=_staff_payload(min_staff=0))
    assert resp.status_code == 422

    # 超过员工总数（2 名员工）
    resp = client.post("/staffing-gaps", json=_staff_payload(min_staff=3))
    assert resp.status_code == 422


def test_staffing_gaps_employee_count_bounds():
    resp = client.post("/staffing-gaps", json=_staff_payload(employees=[]))
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1, employees=[{"id": f"e{i}"} for i in range(21)]
        ),
    )
    assert resp.status_code == 422

    # 20 名合法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1, employees=[{"id": f"e{i}"} for i in range(20)]
        ),
    )
    assert resp.status_code == 200


def test_staffing_gaps_duplicate_employee_ids_422():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(employees=[{"id": "alice"}, {"id": "alice"}]),
    )
    assert resp.status_code == 422


def test_staffing_gaps_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [{**STAFF_SCHED_BERLIN, "timezone": "Not/AZone"}],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [{**STAFF_SCHED_BERLIN, "duration_minutes": 0}],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        {
                            **STAFF_SCHED_BERLIN,
                            "start_date": "2026-01-01",
                            "end_date": "2027-01-03",
                        }
                    ],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422


# --- POST /rest-violations ---------------------------------------------------


def _rest_schedule(local_time: str, duration: int = 60, **overrides) -> dict:
    schedule = {
        "timezone": "UTC",
        "start_date": "2026-03-02",
        "end_date": "2026-03-02",
        "weekdays": [0],
        "local_time": local_time,
        "duration_minutes": duration,
    }
    schedule.update(overrides)
    return schedule


def test_rest_violations_basic():
    # 工作段 [08:00,09:00) 与 [10:00,11:00) UTC，空档 60 分钟 < 阈值 90
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [_rest_schedule("08:00"), _rest_schedule("10:00")],
            "min_rest_minutes": 90,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "min_rest_minutes": 90,
        "count": 1,
        "violations": [
            {
                "start_utc": "2026-03-02T09:00:00Z",
                "end_utc": "2026-03-02T10:00:00Z",
                "rest_minutes": 60.0,
                "shortfall_minutes": 30.0,
            }
        ],
    }


def test_rest_violations_touching_schedules_merged_no_violation():
    # 相接日程 08:00+60 / 09:00+60 合并为连续工作，不产生空档（无负休息）
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [_rest_schedule("08:00"), _rest_schedule("09:00")],
            "min_rest_minutes": 90,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"min_rest_minutes": 90, "count": 0, "violations": []}


def test_rest_violations_exact_threshold_not_violation():
    # 空档恰好 90 分钟，等于阈值不算违规
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [_rest_schedule("08:00"), _rest_schedule("10:30")],
            "min_rest_minutes": 90,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_rest_violations_sorted_multiple():
    # 三段工作 08:00+60 / 09:30+30 / 10:45+60，两个空档 30 / 45 分钟，按起点排序
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule("10:45"),
                _rest_schedule("08:00"),
                _rest_schedule("09:30", 30),
            ],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["violations"] == [
        {
            "start_utc": "2026-03-02T09:00:00Z",
            "end_utc": "2026-03-02T09:30:00Z",
            "rest_minutes": 30.0,
            "shortfall_minutes": 30.0,
        },
        {
            "start_utc": "2026-03-02T10:00:00Z",
            "end_utc": "2026-03-02T10:45:00Z",
            "rest_minutes": 45.0,
            "shortfall_minutes": 15.0,
        },
    ]


def test_rest_violations_cross_timezone():
    # 柏林 09:00 CET = 08:00 UTC 工作 60 分钟；
    # 纽约 04:30 EST = 09:30 UTC 工作 60 分钟 -> 空档 [09:00, 09:30) 30 分钟
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule("09:00", timezone="Europe/Berlin"),
                _rest_schedule("04:30", timezone="America/New_York"),
            ],
            "min_rest_minutes": 45,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["violations"] == [
        {
            "start_utc": "2026-03-02T09:00:00Z",
            "end_utc": "2026-03-02T09:30:00Z",
            "rest_minutes": 30.0,
            "shortfall_minutes": 15.0,
        }
    ]


def test_rest_violations_spring_forward_real_elapsed():
    # 纽约 2026-03-08 春跳（02:00 -> 03:00）：
    # 01:00 EST = 06:00 UTC 工作 60 分钟，04:00 EDT = 08:00 UTC 工作 60 分钟；
    # 钟面相隔 3 小时，真实空档仅 [07:00, 08:00) = 60 分钟
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule(
                    "01:00",
                    timezone="America/New_York",
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    weekdays=[6],
                ),
                _rest_schedule(
                    "04:00",
                    timezone="America/New_York",
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    weekdays=[6],
                ),
            ],
            "min_rest_minutes": 90,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["violations"] == [
        {
            "start_utc": "2026-03-08T07:00:00Z",
            "end_utc": "2026-03-08T08:00:00Z",
            "rest_minutes": 60.0,
            "shortfall_minutes": 30.0,
        }
    ]


def test_rest_violations_fall_back_real_elapsed():
    # 纽约 2026-11-01 秋回（当日 25 小时）：
    # 00:30 EDT = 04:30 UTC 工作 60 分钟，03:00 EST = 08:00 UTC 工作 60 分钟；
    # 真实空档 [05:30, 08:00) = 150 分钟（钟面仅 1.5 小时）
    schedules = [
        _rest_schedule(
            "00:30",
            timezone="America/New_York",
            start_date="2026-11-01",
            end_date="2026-11-01",
            weekdays=[6],
        ),
        _rest_schedule(
            "03:00",
            timezone="America/New_York",
            start_date="2026-11-01",
            end_date="2026-11-01",
            weekdays=[6],
        ),
    ]
    resp = client.post(
        "/rest-violations", json={"schedules": schedules, "min_rest_minutes": 120}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0  # 150 >= 120，达标

    resp = client.post(
        "/rest-violations", json={"schedules": schedules, "min_rest_minutes": 180}
    )
    assert resp.status_code == 200
    assert resp.json()["violations"] == [
        {
            "start_utc": "2026-11-01T05:30:00Z",
            "end_utc": "2026-11-01T08:00:00Z",
            "rest_minutes": 150.0,
            "shortfall_minutes": 30.0,
        }
    ]


def test_rest_violations_fold_both_touching_no_violation():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 60 分钟且相接
    # -> 合并为连续工作 [05:30, 07:30)，无空档、无负休息
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule(
                    "01:30",
                    timezone="America/New_York",
                    start_date="2026-11-01",
                    end_date="2026-11-01",
                    weekdays=[6],
                    fold_strategy="both",
                )
            ],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"min_rest_minutes": 60, "count": 0, "violations": []}


def test_rest_violations_gap_shift_forward():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    # 工作段 [07:30, 08:30) 与 09:00 UTC 起的另一段，空档 30 分钟
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule(
                    "02:30",
                    timezone="America/New_York",
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    weekdays=[6],
                    gap_strategy="shift_forward",
                ),
                _rest_schedule(
                    "09:00",
                    timezone="UTC",
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    weekdays=[6],
                ),
            ],
            "min_rest_minutes": 45,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["violations"] == [
        {
            "start_utc": "2026-03-08T08:30:00Z",
            "end_utc": "2026-03-08T09:00:00Z",
            "rest_minutes": 30.0,
            "shortfall_minutes": 15.0,
        }
    ]


def test_rest_violations_all_skipped_empty():
    # 唯一日程被 skip 策略跳过 -> 无工作段，无违规
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule(
                    "02:30",
                    timezone="America/New_York",
                    start_date="2026-03-08",
                    end_date="2026-03-08",
                    weekdays=[6],
                    gap_strategy="skip",
                )
            ],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"min_rest_minutes": 60, "count": 0, "violations": []}


def test_rest_violations_min_rest_bounds_422():
    payload = {"schedules": [_rest_schedule("08:00")], "min_rest_minutes": 0}
    resp = client.post("/rest-violations", json=payload)
    assert resp.status_code == 422


def test_rest_violations_empty_schedules_422():
    resp = client.post(
        "/rest-violations", json={"schedules": [], "min_rest_minutes": 60}
    )
    assert resp.status_code == 422


def test_rest_violations_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [_rest_schedule("08:00", timezone="Not/AZone")],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [_rest_schedule("08:00", 0)],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/rest-violations",
        json={
            "schedules": [
                _rest_schedule(
                    "08:00", start_date="2026-01-01", end_date="2027-01-03"
                )
            ],
            "min_rest_minutes": 60,
        },
    )
    assert resp.status_code == 422


# --- POST /common-free-slots -------------------------------------------------

# 窗口：2026-03-02 08:00–12:00 UTC
FREE_WINDOW = {
    "window_start_utc": "2026-03-02T08:00:00Z",
    "window_end_utc": "2026-03-02T12:00:00Z",
}


def _free_payload(**overrides):
    payload = {
        **FREE_WINDOW,
        "duration_minutes": 30,
        "step_minutes": 30,
        "participants": [
            {"schedules": [STAFF_SCHED_BERLIN]},  # 忙 [08:00, 09:00) UTC
            {"schedules": [STAFF_SCHED_NY]},  # 忙 [08:30, 09:30) UTC
        ],
    }
    payload.update(overrides)
    return payload


def test_common_free_slots_basic():
    resp = client.post("/common-free-slots", json=_free_payload())
    assert resp.status_code == 200
    body = resp.json()
    # 忙碌并集 [08:00,09:30)，共同空闲 [09:30,12:00)，网格原点 08:00、步长 30
    assert body["count"] == 5
    assert body["slots"] == [
        {"start_utc": "2026-03-02T09:30:00Z", "end_utc": "2026-03-02T10:00:00Z"},
        {"start_utc": "2026-03-02T10:00:00Z", "end_utc": "2026-03-02T10:30:00Z"},
        {"start_utc": "2026-03-02T10:30:00Z", "end_utc": "2026-03-02T11:00:00Z"},
        {"start_utc": "2026-03-02T11:00:00Z", "end_utc": "2026-03-02T11:30:00Z"},
        {"start_utc": "2026-03-02T11:30:00Z", "end_utc": "2026-03-02T12:00:00Z"},
    ]


def test_common_free_slots_empty_schedules_fully_free():
    # null / 空列表 / 缺省 schedules 均表示全程空闲
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            duration_minutes=60,
            step_minutes=60,
            participants=[{"schedules": []}, {"schedules": None}, {}],
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 4
    assert body["slots"][0] == {
        "start_utc": "2026-03-02T08:00:00Z",
        "end_utc": "2026-03-02T09:00:00Z",
    }
    assert body["slots"][-1] == {
        "start_utc": "2026-03-02T11:00:00Z",
        "end_utc": "2026-03-02T12:00:00Z",
    }


def test_common_free_slots_boundary_touch_allowed():
    # alice 忙 [08:00,09:00)，bob 忙 [10:00,11:00)：预约与忙碌两端相接仍允许
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            duration_minutes=60,
            step_minutes=60,
            participants=[
                {"schedules": [_utc_schedule("08:00")]},
                {"schedules": [_utc_schedule("10:00")]},
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["slots"] == [
        {"start_utc": "2026-03-02T09:00:00Z", "end_utc": "2026-03-02T10:00:00Z"},
        {"start_utc": "2026-03-02T11:00:00Z", "end_utc": "2026-03-02T12:00:00Z"},
    ]


def test_common_free_slots_grid_origin_is_window_start():
    # 窗口起点 08:07 不对齐整点：网格从 08:07 起算
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            window_start_utc="2026-03-02T08:07:00Z",
            window_end_utc="2026-03-02T09:07:00Z",
            duration_minutes=20,
            step_minutes=20,
            participants=[{"schedules": []}],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["slots"] == [
        {"start_utc": "2026-03-02T08:07:00Z", "end_utc": "2026-03-02T08:27:00Z"},
        {"start_utc": "2026-03-02T08:27:00Z", "end_utc": "2026-03-02T08:47:00Z"},
        {"start_utc": "2026-03-02T08:47:00Z", "end_utc": "2026-03-02T09:07:00Z"},
    ]


def test_common_free_slots_fold_both_not_double_occupied():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 90 分钟：
    # 重复实例合并为忙碌 [05:30, 08:00)，不重复占用 -> 共同空闲 [08:00, 09:00)
    resp = client.post(
        "/common-free-slots",
        json={
            "window_start_utc": "2026-11-01T05:00:00Z",
            "window_end_utc": "2026-11-01T09:00:00Z",
            "duration_minutes": 60,
            "step_minutes": 60,
            "participants": [
                {
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-11-01",
                            "end_date": "2026-11-01",
                            "weekdays": [6],
                            "local_time": "01:30",
                            "fold_strategy": "both",
                            "duration_minutes": 90,
                        }
                    ]
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["slots"] == [
        {"start_utc": "2026-11-01T08:00:00Z", "end_utc": "2026-11-01T09:00:00Z"}
    ]


def test_common_free_slots_reuses_gap_shift_forward():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    resp = client.post(
        "/common-free-slots",
        json={
            "window_start_utc": "2026-03-08T07:00:00Z",
            "window_end_utc": "2026-03-08T10:00:00Z",
            "duration_minutes": 30,
            "step_minutes": 30,
            "participants": [
                {
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-03-08",
                            "end_date": "2026-03-08",
                            "weekdays": [6],
                            "local_time": "02:30",
                            "gap_strategy": "shift_forward",
                            "duration_minutes": 60,
                        }
                    ]
                }
            ],
        },
    )
    assert resp.status_code == 200
    # 忙碌 [07:30,08:30)；共同空闲 [07:00,07:30) 与 [08:30,10:00)
    assert resp.json()["slots"] == [
        {"start_utc": "2026-03-08T07:00:00Z", "end_utc": "2026-03-08T07:30:00Z"},
        {"start_utc": "2026-03-08T08:30:00Z", "end_utc": "2026-03-08T09:00:00Z"},
        {"start_utc": "2026-03-08T09:00:00Z", "end_utc": "2026-03-08T09:30:00Z"},
        {"start_utc": "2026-03-08T09:30:00Z", "end_utc": "2026-03-08T10:00:00Z"},
    ]


def test_common_free_slots_window_must_be_z_utc_422():
    # 缺少 Z 后缀 / 用 +00:00 代替 Z 均非法
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(window_start_utc="2026-03-02T08:00:00"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/common-free-slots",
        json=_free_payload(window_end_utc="2026-03-02T12:00:00+00:00"),
    )
    assert resp.status_code == 422


def test_common_free_slots_window_not_increasing_422():
    # 相等与逆序都非法
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(window_end_utc="2026-03-02T08:00:00Z"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            window_start_utc="2026-03-02T12:00:00Z",
            window_end_utc="2026-03-02T08:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_common_free_slots_window_over_31_days_422():
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2026-02-02T00:00:00Z",  # 32 天
        ),
    )
    assert resp.status_code == 422


def test_common_free_slots_window_exactly_31_days_ok():
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2026-02-01T00:00:00Z",  # 恰好 31 天
            participants=[{"schedules": []}],
        ),
    )
    assert resp.status_code == 200


def test_common_free_slots_duration_and_step_bounds_422():
    resp = client.post("/common-free-slots", json=_free_payload(duration_minutes=0))
    assert resp.status_code == 422

    resp = client.post("/common-free-slots", json=_free_payload(duration_minutes=1441))
    assert resp.status_code == 422

    resp = client.post("/common-free-slots", json=_free_payload(step_minutes=0))
    assert resp.status_code == 422

    resp = client.post("/common-free-slots", json=_free_payload(step_minutes=1441))
    assert resp.status_code == 422

    # 边界值 1 与 1440 合法
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            duration_minutes=1440,
            step_minutes=1440,
            window_start_utc="2026-03-02T00:00:00Z",
            window_end_utc="2026-03-04T00:00:00Z",
            participants=[{"schedules": []}],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_common_free_slots_participant_count_bounds():
    resp = client.post("/common-free-slots", json=_free_payload(participants=[]))
    assert resp.status_code == 422

    resp = client.post(
        "/common-free-slots",
        json=_free_payload(participants=[{} for _ in range(11)]),
    )
    assert resp.status_code == 422

    # 10 名合法
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(participants=[{} for _ in range(10)]),
    )
    assert resp.status_code == 200


def test_common_free_slots_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            participants=[{"schedules": [{**STAFF_SCHED_BERLIN, "timezone": "Not/AZone"}]}]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            participants=[{"schedules": [{**STAFF_SCHED_BERLIN, "duration_minutes": 0}]}]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/common-free-slots",
        json=_free_payload(
            participants=[
                {
                    "schedules": [
                        {
                            **STAFF_SCHED_BERLIN,
                            "start_date": "2026-01-01",
                            "end_date": "2027-01-03",
                        }
                    ]
                }
            ]
        ),
    )
    assert resp.status_code == 422


# --- POST /schedule-changes --------------------------------------------------

# 窗口：2026-03-02 全天（UTC）
CHANGE_WINDOW = {
    "window_start_utc": "2026-03-02T00:00:00Z",
    "window_end_utc": "2026-03-03T00:00:00Z",
}


def _change_payload(**overrides):
    payload = {
        **CHANGE_WINDOW,
        "before": [STAFF_SCHED_BERLIN],  # 工作 [08:00, 09:00) UTC
        "after": [STAFF_SCHED_NY],  # 工作 [08:30, 09:30) UTC
    }
    payload.update(overrides)
    return payload


def test_schedule_changes_basic():
    resp = client.post("/schedule-changes", json=_change_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "cancelled_count": 1,
        "cancelled_total_minutes": 30.0,
        "cancelled": [
            {
                "start_utc": "2026-03-02T08:00:00Z",
                "end_utc": "2026-03-02T08:30:00Z",
                "minutes": 30.0,
            }
        ],
        "added_count": 1,
        "added_total_minutes": 30.0,
        "added": [
            {
                "start_utc": "2026-03-02T09:00:00Z",
                "end_utc": "2026-03-02T09:30:00Z",
                "minutes": 30.0,
            }
        ],
    }


def test_schedule_changes_same_coverage_empty_diff():
    # 仅实例条目变化：一段 120 分钟 vs 两段相接 60 分钟 -> 覆盖相同，空差异
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            before=[_utc_schedule("08:00", 120)],
            after=[_utc_schedule("08:00"), _utc_schedule("09:00")],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "cancelled_count": 0,
        "cancelled_total_minutes": 0.0,
        "cancelled": [],
        "added_count": 0,
        "added_total_minutes": 0.0,
        "added": [],
    }


def test_schedule_changes_null_sides():
    # 变更前无排班 -> 全部为新增
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(before=None, after=[_utc_schedule("08:00")]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == []
    assert body["added"] == [
        {
            "start_utc": "2026-03-02T08:00:00Z",
            "end_utc": "2026-03-02T09:00:00Z",
            "minutes": 60.0,
        }
    ]
    assert body["added_total_minutes"] == 60.0

    # 变更后无排班（缺省字段视同 null）-> 全部为取消
    resp = client.post(
        "/schedule-changes",
        json={**CHANGE_WINDOW, "before": [_utc_schedule("08:00")]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] == []
    assert body["cancelled"] == [
        {
            "start_utc": "2026-03-02T08:00:00Z",
            "end_utc": "2026-03-02T09:00:00Z",
            "minutes": 60.0,
        }
    ]
    assert body["cancelled_total_minutes"] == 60.0

    # 两侧均无排班 -> 空差异
    resp = client.post(
        "/schedule-changes",
        json={**CHANGE_WINDOW, "before": [], "after": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "cancelled_count": 0,
        "cancelled_total_minutes": 0.0,
        "cancelled": [],
        "added_count": 0,
        "added_total_minutes": 0.0,
        "added": [],
    }


def test_schedule_changes_clips_to_window():
    # 实例 2026-03-01 23:00 UTC 起 120 分钟，仅 [00:00, 01:00) 落在窗口内
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            before=[
                {
                    "timezone": "UTC",
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-01",
                    "weekdays": [6],
                    "local_time": "23:00",
                    "duration_minutes": 120,
                }
            ],
            after=[],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T01:00:00Z",
            "minutes": 60.0,
        }
    ]


def test_schedule_changes_sorted_multiple_segments():
    # 变更前 08:00+60 / 12:00+60，变更后 08:30+60 / 13:30+60：
    # 取消 [08:00,08:30) 与 [12:00,13:00)，新增 [09:00,09:30) 与 [13:30,14:30)，各自按起点排序
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            before=[_utc_schedule("12:00"), _utc_schedule("08:00")],
            after=[_utc_schedule("13:30"), _utc_schedule("08:30")],
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] == [
        {
            "start_utc": "2026-03-02T08:00:00Z",
            "end_utc": "2026-03-02T08:30:00Z",
            "minutes": 30.0,
        },
        {
            "start_utc": "2026-03-02T12:00:00Z",
            "end_utc": "2026-03-02T13:00:00Z",
            "minutes": 60.0,
        },
    ]
    assert body["added"] == [
        {
            "start_utc": "2026-03-02T09:00:00Z",
            "end_utc": "2026-03-02T09:30:00Z",
            "minutes": 30.0,
        },
        {
            "start_utc": "2026-03-02T13:30:00Z",
            "end_utc": "2026-03-02T14:30:00Z",
            "minutes": 60.0,
        },
    ]
    assert body["cancelled_total_minutes"] == 90.0
    assert body["added_total_minutes"] == 90.0


def test_schedule_changes_reuses_fold_both():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 90 分钟：
    # 变更前合并为连续工作 [05:30, 08:00) = 150 分钟，变更后无排班
    resp = client.post(
        "/schedule-changes",
        json={
            "window_start_utc": "2026-11-01T00:00:00Z",
            "window_end_utc": "2026-11-02T00:00:00Z",
            "before": [
                {
                    "timezone": "America/New_York",
                    "start_date": "2026-11-01",
                    "end_date": "2026-11-01",
                    "weekdays": [6],
                    "local_time": "01:30",
                    "fold_strategy": "both",
                    "duration_minutes": 90,
                }
            ],
            "after": None,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["cancelled"] == [
        {
            "start_utc": "2026-11-01T05:30:00Z",
            "end_utc": "2026-11-01T08:00:00Z",
            "minutes": 150.0,
        }
    ]


def test_schedule_changes_reuses_gap_shift_forward():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    resp = client.post(
        "/schedule-changes",
        json={
            "window_start_utc": "2026-03-08T00:00:00Z",
            "window_end_utc": "2026-03-09T00:00:00Z",
            "before": [],
            "after": [
                {
                    "timezone": "America/New_York",
                    "start_date": "2026-03-08",
                    "end_date": "2026-03-08",
                    "weekdays": [6],
                    "local_time": "02:30",
                    "gap_strategy": "shift_forward",
                    "duration_minutes": 60,
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == [
        {
            "start_utc": "2026-03-08T07:30:00Z",
            "end_utc": "2026-03-08T08:30:00Z",
            "minutes": 60.0,
        }
    ]


def test_schedule_changes_window_must_be_z_utc_422():
    # 缺少 Z 后缀 / 用 +00:00 代替 Z 均非法
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(window_start_utc="2026-03-02T00:00:00"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/schedule-changes",
        json=_change_payload(window_end_utc="2026-03-03T00:00:00+00:00"),
    )
    assert resp.status_code == 422


def test_schedule_changes_window_not_increasing_422():
    # 相等与逆序都非法
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(window_end_utc="2026-03-02T00:00:00Z"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            window_start_utc="2026-03-03T00:00:00Z",
            window_end_utc="2026-03-02T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_schedule_changes_window_over_31_days_422():
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2026-02-02T00:00:00Z",  # 32 天
        ),
    )
    assert resp.status_code == 422


def test_schedule_changes_window_exactly_31_days_ok():
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2026-02-01T00:00:00Z",  # 恰好 31 天
            before=[],
            after=[],
        ),
    )
    assert resp.status_code == 200


def test_schedule_changes_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/schedule-changes",
        json=_change_payload(before=[{**STAFF_SCHED_BERLIN, "timezone": "Not/AZone"}]),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/schedule-changes",
        json=_change_payload(after=[{**STAFF_SCHED_NY, "duration_minutes": 0}]),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/schedule-changes",
        json=_change_payload(
            before=[
                {
                    **STAFF_SCHED_BERLIN,
                    "start_date": "2026-01-01",
                    "end_date": "2027-01-03",
                }
            ]
        ),
    )
    assert resp.status_code == 422


# --- POST /handoff-route -----------------------------------------------------

HANDOFF_WINDOW = {
    "message_at_utc": "2026-03-02T00:00:00Z",
    "deadline_utc": "2026-03-03T00:00:00Z",
}


def _handoff_payload(**overrides):
    payload = {
        **HANDOFF_WINDOW,
        "duration_minutes": 30,
        "holder_id": "alice",
        "target_id": "carol",
        "employees": [
            {"id": "alice", "schedules": [_utc_schedule("08:00", 120)]},
            {
                "id": "bob",
                "schedules": [_utc_schedule("08:30", 60), _utc_schedule("11:00", 60)],
            },
            {"id": "carol", "schedules": [_utc_schedule("11:00", 120)]},
        ],
    }
    payload.update(overrides)
    return payload


def test_handoff_route_multihop_chain():
    # alice [08,10) -> bob 共同段 [08:30,09:30)，bob 09:00 持有；
    # bob [11,12) 与 carol [11,13) -> carol 11:30 获得
    resp = client.post("/handoff-route", json=_handoff_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "arrival_utc": "2026-03-02T11:30:00Z",
        "count": 2,
        "chain": [
            {
                "sender_id": "alice",
                "receiver_id": "bob",
                "start_utc": "2026-03-02T08:30:00Z",
                "end_utc": "2026-03-02T09:00:00Z",
            },
            {
                "sender_id": "bob",
                "receiver_id": "carol",
                "start_utc": "2026-03-02T11:00:00Z",
                "end_utc": "2026-03-02T11:30:00Z",
            },
        ],
    }


def test_handoff_route_holder_is_target_empty_chain():
    resp = client.post(
        "/handoff-route", json=_handoff_payload(target_id="alice")
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "arrival_utc": "2026-03-02T00:00:00Z",
        "count": 0,
        "chain": [],
    }


def test_handoff_route_no_solution_null_empty_chain():
    # alice 与 carol 共同在岗仅 20 分钟，交接需 30 分钟 -> 无解
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00", 60)]},
                {"id": "carol", "schedules": [_utc_schedule("08:40", 20)]},
            ]
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"arrival_utc": None, "count": 0, "chain": []}


def test_handoff_route_touching_shifts_merge():
    # alice 两段相接 [08,09)+[09,10) 合并为连续在岗，才能与 carol
    # [09:30, 10:30) 在共同段 [09:30,10) 完成 30 分钟交接
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [_utc_schedule("08:00"), _utc_schedule("09:00")],
                },
                {"id": "carol", "schedules": [_utc_schedule("09:30", 60)]},
            ]
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "arrival_utc": "2026-03-02T10:00:00Z",
        "count": 1,
        "chain": [
            {
                "sender_id": "alice",
                "receiver_id": "carol",
                "start_utc": "2026-03-02T09:30:00Z",
                "end_utc": "2026-03-02T10:00:00Z",
            }
        ],
    }


def test_handoff_route_message_ready_caps_start():
    # 消息 08:45 才产生：alice 虽 08:00 起在岗，交接不得早于 08:45
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            message_at_utc="2026-03-02T08:45:00Z",
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "carol", "schedules": [_utc_schedule("08:30", 120)]},
            ],
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["arrival_utc"] == "2026-03-02T09:15:00Z"
    assert body["chain"][0]["start_utc"] == "2026-03-02T08:45:00Z"


def test_handoff_route_null_schedules_never_on_duty():
    # carol 无排班 -> 永远无法接收 -> 无解；alice 无排班同样无解
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "carol", "schedules": None},
            ]
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"arrival_utc": None, "count": 0, "chain": []}


def test_handoff_route_finish_exactly_at_deadline_ok():
    # 共同段 [08:30, 09:00)，交接终点 09:00 恰好等于截止 -> 有解
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            deadline_utc="2026-03-02T09:00:00Z",
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00", 60)]},
                {"id": "carol", "schedules": [_utc_schedule("08:30", 60)]},
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["arrival_utc"] == "2026-03-02T09:00:00Z"


def test_handoff_route_reuses_gap_fold_expansion():
    # 纽约 2026-11-01 01:30 秋回两次，各 60 分钟相接 -> 合并为 [05:30,07:30)
    resp = client.post(
        "/handoff-route",
        json={
            "message_at_utc": "2026-11-01T00:00:00Z",
            "deadline_utc": "2026-11-02T00:00:00Z",
            "duration_minutes": 90,
            "holder_id": "alice",
            "target_id": "carol",
            "employees": [
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-11-01",
                            "end_date": "2026-11-01",
                            "weekdays": [6],
                            "local_time": "01:30",
                            "fold_strategy": "both",
                            "duration_minutes": 60,
                        }
                    ],
                },
                {
                    "id": "carol",
                    "schedules": [
                        {
                            "timezone": "UTC",
                            "start_date": "2026-11-01",
                            "end_date": "2026-11-01",
                            "weekdays": [6],
                            "local_time": "05:30",
                            "duration_minutes": 120,
                        }
                    ],
                },
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # 合并后共同在岗 [05:30,07:30)，可完成 90 分钟交接 -> 07:00 到达
    assert body["arrival_utc"] == "2026-11-01T07:00:00Z"
    assert body["count"] == 1


def test_handoff_route_window_must_be_z_utc_422():
    resp = client.post(
        "/handoff-route", json=_handoff_payload(message_at_utc="2026-03-02T00:00:00")
    )
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(deadline_utc="2026-03-03T00:00:00+00:00"),
    )
    assert resp.status_code == 422


def test_handoff_route_window_not_increasing_422():
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(deadline_utc="2026-03-02T00:00:00Z"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            message_at_utc="2026-03-03T00:00:00Z",
            deadline_utc="2026-03-02T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_handoff_route_window_over_31_days_422():
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            message_at_utc="2026-01-01T00:00:00Z",
            deadline_utc="2026-02-02T00:00:00Z",  # 32 天
        ),
    )
    assert resp.status_code == 422


def test_handoff_route_window_exactly_31_days_ok():
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            message_at_utc="2026-01-01T00:00:00Z",
            deadline_utc="2026-02-01T00:00:00Z",  # 恰好 31 天
        ),
    )
    assert resp.status_code == 200


def test_handoff_route_duration_bounds_422():
    resp = client.post("/handoff-route", json=_handoff_payload(duration_minutes=0))
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route", json=_handoff_payload(duration_minutes=1441)
    )
    assert resp.status_code == 422

    # 边界值 1 与 1440 合法（1440 需窗口 >= 1 天，31 天窗口满足）
    resp = client.post(
        "/handoff-route", json=_handoff_payload(duration_minutes=1)
    )
    assert resp.status_code == 200
    resp = client.post(
        "/handoff-route", json=_handoff_payload(duration_minutes=1440)
    )
    assert resp.status_code == 200


def test_handoff_route_employee_count_bounds():
    resp = client.post("/handoff-route", json=_handoff_payload(employees=[]))
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[{"id": f"e{i}"} for i in range(21)],
            holder_id="e0",
            target_id="e1",
        ),
    )
    assert resp.status_code == 422

    # 20 名合法
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[{"id": f"e{i}"} for i in range(20)],
            holder_id="e0",
            target_id="e19",
        ),
    )
    assert resp.status_code == 200


def test_handoff_route_duplicate_ids_422():
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[{"id": "alice"}, {"id": "alice"}, {"id": "carol"}]
        ),
    )
    assert resp.status_code == 422


def test_handoff_route_unknown_reference_422():
    resp = client.post(
        "/handoff-route", json=_handoff_payload(holder_id="zoe")
    )
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route", json=_handoff_payload(target_id="zoe")
    )
    assert resp.status_code == 422


def test_handoff_route_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {"id": "alice", "schedules": [{**STAFF_SCHED_BERLIN, "timezone": "X/Y"}]},
                {"id": "carol"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {"id": "alice", "schedules": [{**STAFF_SCHED_BERLIN, "duration_minutes": 0}]},
                {"id": "carol"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/handoff-route",
        json=_handoff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        {
                            **STAFF_SCHED_BERLIN,
                            "start_date": "2026-01-01",
                            "end_date": "2027-01-03",
                        }
                    ],
                },
                {"id": "carol"},
            ]
        ),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /inventory-allocation：FEFO 库存批次分配
# ---------------------------------------------------------------------------

_INV_BASE_BATCHES = [
    {"batch_no": "B9", "available_quantity": 100, "expiry_date": "2026-06-01"},
    {"batch_no": "B10", "available_quantity": 3, "expiry_date": "2026-03-01"},
    {"batch_no": "B02", "available_quantity": 5, "expiry_date": "2026-03-01"},
    {"batch_no": "B01", "available_quantity": 7, "expiry_date": "2026-01-01"},
]


def _inv_payload(orders, batches=_INV_BASE_BATCHES):
    return {"batches": batches, "orders": orders}


def test_inventory_allocation_fefo_split_and_remaining():
    resp = client.post(
        "/inventory-allocation",
        json=_inv_payload([{"order_id": "O1", "quantity": 10}]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_allocated"] == 10
    assert body["allocations"] == [
        {
            "order_id": "O1",
            "quantity": 10,
            "lines": [
                {"batch_no": "B01", "expiry_date": "2026-01-01", "quantity": 7},
                {"batch_no": "B02", "expiry_date": "2026-03-01", "quantity": 3},
            ],
        }
    ]
    # 剩余库存按 FEFO 顺序返回
    assert [(r["batch_no"], r["remaining"]) for r in body["remaining_stock"]] == [
        ("B01", 0),
        ("B02", 2),
        ("B10", 3),
        ("B9", 100),
    ]
    b01 = next(r for r in body["remaining_stock"] if r["batch_no"] == "B01")
    assert b01["available_before"] == 7


def test_inventory_allocation_multiple_orders_share_stock():
    resp = client.post(
        "/inventory-allocation",
        json=_inv_payload(
            [
                {"order_id": "O1", "quantity": 8},
                {"order_id": "O2", "quantity": 7},
            ]
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_allocated"] == 15
    assert [
        (l["batch_no"], l["quantity"]) for l in body["allocations"][0]["lines"]
    ] == [("B01", 7), ("B02", 1)]
    assert [
        (l["batch_no"], l["quantity"]) for l in body["allocations"][1]["lines"]
    ] == [("B02", 4), ("B10", 3)]


def test_inventory_allocation_decimal_quantities():
    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "A", "available_quantity": "2.5", "expiry_date": "2026-01-01"},
                {"batch_no": "B", "available_quantity": "3.25", "expiry_date": "2026-02-01"},
            ],
            "orders": [{"order_id": "O1", "quantity": "5.75"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [
        (l["batch_no"], l["quantity"]) for l in body["allocations"][0]["lines"]
    ] == [("A", 2.5), ("B", 3.25)]
    assert body["total_allocated"] == 5.75


def test_inventory_allocation_insufficient_stock_409_no_partial_result():
    resp = client.post(
        "/inventory-allocation",
        json=_inv_payload(
            [
                {"order_id": "O1", "quantity": 10},
                {"order_id": "O2", "quantity": 1000},
            ]
        ),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail == {
        "error": "insufficient_stock",
        "order_id": "O2",
        "requested": 1000,
        "shortfall": 895,
        "total_available": 115,
    }
    # 不返回任何部分分配字段
    assert "allocations" not in resp.json()
    assert "remaining_stock" not in resp.json()


def test_inventory_allocation_first_order_insufficient_409():
    resp = client.post(
        "/inventory-allocation",
        json=_inv_payload([{"order_id": "O1", "quantity": 1000}]),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["order_id"] == "O1"
    assert resp.json()["detail"]["shortfall"] == 885


def test_inventory_allocation_non_positive_quantity_422():
    for bad in (0, -1, "-0.01"):
        resp = client.post(
            "/inventory-allocation",
            json=_inv_payload(
                [{"order_id": "O1", "quantity": 10}],
                batches=[
                    {"batch_no": "A", "available_quantity": bad, "expiry_date": "2026-01-01"}
                ],
            ),
        )
        assert resp.status_code == 422, bad

    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "A", "available_quantity": 5, "expiry_date": "2026-01-01"}
            ],
            "orders": [{"order_id": "O1", "quantity": 0}],
        },
    )
    assert resp.status_code == 422


def test_inventory_allocation_duplicate_batch_no_422():
    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "A", "available_quantity": 5, "expiry_date": "2026-01-01"},
                {"batch_no": "A", "available_quantity": 3, "expiry_date": "2026-02-01"},
            ],
            "orders": [{"order_id": "O1", "quantity": 1}],
        },
    )
    assert resp.status_code == 422


def test_inventory_allocation_duplicate_order_id_422():
    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "A", "available_quantity": 5, "expiry_date": "2026-01-01"}
            ],
            "orders": [
                {"order_id": "O1", "quantity": 1},
                {"order_id": "O1", "quantity": 1},
            ],
        },
    )
    assert resp.status_code == 422


def test_inventory_allocation_bad_date_format_422():
    for bad in ("2026/01/01", "01-01-2026", "2026-1-1", "20260101", "not-a-date", "2026-02-30"):
        resp = client.post(
            "/inventory-allocation",
            json={
                "batches": [
                    {"batch_no": "A", "available_quantity": 5, "expiry_date": bad}
                ],
                "orders": [{"order_id": "O1", "quantity": 1}],
            },
        )
        assert resp.status_code == 422, bad


def test_inventory_allocation_empty_lists_and_missing_fields_422():
    resp = client.post("/inventory-allocation", json={"batches": [], "orders": []})
    assert resp.status_code == 422

    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "   ", "available_quantity": 5, "expiry_date": "2026-01-01"}
            ],
            "orders": [{"order_id": "O1", "quantity": 1}],
        },
    )
    assert resp.status_code == 422

    resp = client.post("/inventory-allocation", json={})
    assert resp.status_code == 422


def test_inventory_allocation_non_numeric_quantity_422():
    resp = client.post(
        "/inventory-allocation",
        json={
            "batches": [
                {"batch_no": "A", "available_quantity": "abc", "expiry_date": "2026-01-01"}
            ],
            "orders": [{"order_id": "O1", "quantity": 1}],
        },
    )
    assert resp.status_code == 422


def test_inventory_allocation_non_finite_quantity_422():
    # NaN / Infinity 不是合法 JSON 数字；以非标准 token 发送时服务端解析亦拒绝
    for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
        raw = (
            '{"batches": [{"batch_no": "A", "available_quantity": '
            + token
            + ', "expiry_date": "2026-01-01"}], '
            '"orders": [{"order_id": "O1", "quantity": 1}]}'
        )
        resp = client.post(
            "/inventory-allocation",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, token


# ---------------------------------------------------------------------------
# /receipt-write-off：收款（含撤回）对发票的核销试算
# ---------------------------------------------------------------------------

_WO_INVOICES = [
    {"invoice_no": "INV-010", "due_date": "2026-02-01", "receivable": 10},
    {"invoice_no": "INV-002", "due_date": "2026-01-01", "receivable": 7},
    {"invoice_no": "INV-001", "due_date": "2026-01-01", "receivable": 5},
]


def _wo(events, invoices=_WO_INVOICES):
    return client.post(
        "/receipt-write-off", json={"invoices": invoices, "events": events}
    )


def test_write_off_fills_by_due_date_then_invoice_no_and_splits():
    resp = _wo([{"type": "receipt", "event_id": "R1", "amount": 20}])
    assert resp.status_code == 200
    body = resp.json()
    assert body["receipts"] == [
        {
            "event_id": "R1",
            "amount": 20,
            "lines": [
                {"invoice_no": "INV-001", "due_date": "2026-01-01", "amount": 5},
                {"invoice_no": "INV-002", "due_date": "2026-01-01", "amount": 7},
                {"invoice_no": "INV-010", "due_date": "2026-02-01", "amount": 8},
            ],
        }
    ]
    # 未结表按 (到期日, 发票号) 排序
    assert [
        (inv["invoice_no"], inv["outstanding"]) for inv in body["invoices"]
    ] == [
        ("INV-001", 0),
        ("INV-002", 0),
        ("INV-010", 2),
    ]
    assert body["total_written_off"] == 20
    # 原始应收金额原样返回
    assert {inv["invoice_no"]: inv["receivable"] for inv in body["invoices"]} == {
        "INV-001": 5,
        "INV-002": 7,
        "INV-010": 10,
    }


def test_write_off_multiple_receipts_share_outstanding_in_order():
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 12},
            {"type": "receipt", "event_id": "R2", "amount": "8"},
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [(l["invoice_no"], l["amount"]) for l in body["receipts"][0]["lines"]] == [
        ("INV-001", 5),
        ("INV-002", 7),
    ]
    assert [(l["invoice_no"], l["amount"]) for l in body["receipts"][1]["lines"]] == [
        ("INV-010", 8)
    ]
    assert body["total_written_off"] == 20


def test_write_off_exact_equal_receipt_settles_everything():
    resp = _wo(
        [{"type": "receipt", "event_id": "R1", "amount": "22.00"}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": "2.50"},
            {"invoice_no": "B", "due_date": "2026-02-01", "receivable": "19.50"},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(inv["outstanding"] == 0 for inv in body["invoices"])
    assert body["total_written_off"] == 22.0


def test_write_off_decimal_cents_serialized_as_number():
    resp = _wo(
        [{"type": "receipt", "event_id": "R1", "amount": "0.15"}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": "0.10"},
            {"invoice_no": "B", "due_date": "2026-02-01", "receivable": "0.20"},
        ],
    )
    assert resp.status_code == 200
    lines = resp.json()["receipts"][0]["lines"]
    assert [(l["invoice_no"], l["amount"]) for l in lines] == [
        ("A", 0.1),
        ("B", 0.05),
    ]


def test_write_off_withdrawal_releases_only_target_for_later_receipt():
    # R1 结清 A(10)；R2 核销 B(5)；撤回 R1 释放 A；R3 使用释放额度核销 A
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
        {"invoice_no": "B", "due_date": "2026-02-01", "receivable": 10},
    ]
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 10},
            {"type": "receipt", "event_id": "R2", "amount": 5},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "receipt", "event_id": "R3", "amount": 10},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 已撤回的 R1 不在有效收款明细中
    assert [r["event_id"] for r in body["receipts"]] == ["R2", "R3"]
    assert [(l["invoice_no"], l["amount"]) for l in body["receipts"][0]["lines"]] == [
        ("B", 5)
    ]
    assert [(l["invoice_no"], l["amount"]) for l in body["receipts"][1]["lines"]] == [
        ("A", 10)
    ]
    assert [(inv["invoice_no"], inv["outstanding"]) for inv in body["invoices"]] == [
        ("A", 0),
        ("B", 5),
    ]
    assert body["total_written_off"] == 15


def test_write_off_withdrawal_of_split_receipt_restores_all_lines():
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 12},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
        ]
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["receipts"] == []
    assert [inv["outstanding"] for inv in body["invoices"]] == [5, 7, 10]
    assert body["total_written_off"] == 0


def test_write_off_identical_receipt_replay_applied_once():
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
    ]
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 8},
            {"type": "receipt", "event_id": "R1", "amount": "8.00"},
            {"type": "receipt", "event_id": "R2", "amount": 2},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [r["event_id"] for r in body["receipts"]] == ["R1", "R2"]
    assert body["receipts"][0]["lines"] == [
        {"invoice_no": "A", "due_date": "2026-01-01", "amount": 8}
    ]
    assert body["invoices"][0]["outstanding"] == 0


def test_write_off_identical_reversal_replay_applied_once():
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
    ]
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 5},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "receipt", "event_id": "R2", "amount": 5},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [r["event_id"] for r in body["receipts"]] == ["R2"]
    assert body["invoices"][0]["outstanding"] == 5


def test_write_off_different_replay_content_rejected_409():
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
    ]
    # 金额不同
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 5},
            {"type": "receipt", "event_id": "R1", "amount": 6},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "inconsistent_replay",
        "event_id": "R1",
    }
    assert "receipts" not in resp.json()

    # 同事件号类型互换（收款 vs 撤回）
    resp = _wo(
        [
            {"type": "receipt", "event_id": "E1", "amount": 5},
            {"type": "reversal", "event_id": "E1", "reversal_of": "R9"},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "inconsistent_replay"

    # 撤回目标不同
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 1},
            {"type": "receipt", "event_id": "R2", "amount": 1},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R2"},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["event_id"] == "V1"


def test_write_off_reversal_missing_target_rejected_409():
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
    ]
    resp = _wo(
        [{"type": "reversal", "event_id": "V1", "reversal_of": "R1"}],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "reversal_target_invalid",
        "event_id": "V1",
        "referenced_event_id": "R1",
        "reason": "not_found",
    }

    # 前向引用：目标收款事件出现在撤回之后
    resp = _wo(
        [
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "receipt", "event_id": "R1", "amount": 1},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_found"

    # 撤回指向另一个撤回事件
    resp = _wo(
        [
            {"type": "reversal", "event_id": "V1", "reversal_of": "V2"},
            {"type": "reversal", "event_id": "V2", "reversal_of": "X"},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["event_id"] == "V1"


def test_write_off_reversal_already_reversed_rejected_409():
    invoices = [
        {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
    ]
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 1},
            {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
            {"type": "reversal", "event_id": "V2", "reversal_of": "R1"},
        ],
        invoices=invoices,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "reversal_target_invalid",
        "event_id": "V2",
        "referenced_event_id": "R1",
        "reason": "already_reversed",
    }


def test_write_off_receipt_exceeding_remaining_rejected_409_no_partial():
    resp = _wo(
        [{"type": "receipt", "event_id": "R1", "amount": "22.01"}],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "receipt_exceeds_receivable",
        "event_id": "R1",
        "amount": 22.01,
        "remaining_receivable": 22,
        "shortfall": 0.01,
    }
    body = resp.json()
    assert "receipts" not in body
    assert "invoices" not in body

    # 超额发生在后续收款（首笔已结清全部发票）
    resp = _wo(
        [
            {"type": "receipt", "event_id": "R1", "amount": 22},
            {"type": "receipt", "event_id": "R2", "amount": 0.01},
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["event_id"] == "R2"


def test_write_off_non_positive_or_more_than_two_decimals_422():
    for bad_amount in (0, -1, "0", "-0.01", "0.001", "10.123", "abc"):
        resp = _wo(
            [{"type": "receipt", "event_id": "R1", "amount": bad_amount}],
            invoices=[
                {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10}
            ],
        )
        assert resp.status_code == 422, bad_amount

    for bad_receivable in (0, -5, "0.005", "1.999"):
        resp = _wo(
            [{"type": "receipt", "event_id": "R1", "amount": 1}],
            invoices=[
                {"invoice_no": "A", "due_date": "2026-01-01", "receivable": bad_receivable}
            ],
        )
        assert resp.status_code == 422, bad_receivable


def test_write_off_bad_date_and_duplicate_invoice_and_missing_fields_422():
    for bad_date in ("2026/01/01", "01-01-2026", "2026-1-1", "20260101", "2026-02-30"):
        resp = _wo(
            [{"type": "receipt", "event_id": "R1", "amount": 1}],
            invoices=[
                {"invoice_no": "A", "due_date": bad_date, "receivable": 10}
            ],
        )
        assert resp.status_code == 422, bad_date

    # 发票号重复
    resp = _wo(
        [{"type": "receipt", "event_id": "R1", "amount": 1}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10},
            {"invoice_no": "A", "due_date": "2026-01-02", "receivable": 1},
        ],
    )
    assert resp.status_code == 422

    # 空事件号 / 空发票号 / 空白事件号
    resp = _wo(
        [{"type": "receipt", "event_id": "  ", "amount": 1}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10}
        ],
    )
    assert resp.status_code == 422

    resp = _wo(
        [{"type": "receipt", "event_id": "R1", "amount": 1}],
        invoices=[
            {"invoice_no": "   ", "due_date": "2026-01-01", "receivable": 10}
        ],
    )
    assert resp.status_code == 422

    # 撤回缺少 reversal_of
    resp = _wo(
        [{"type": "reversal", "event_id": "V1"}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10}
        ],
    )
    assert resp.status_code == 422


def test_write_off_unknown_type_discriminator_422():
    resp = _wo(
        [{"type": "chargeback", "event_id": "X", "amount": 1}],
        invoices=[
            {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10}
        ],
    )
    assert resp.status_code == 422


def test_write_off_empty_lists_and_missing_body_422():
    assert client.post("/receipt-write-off", json={"invoices": [], "events": []}).status_code == 422
    assert client.post("/receipt-write-off", json={}).status_code == 422
    assert client.post(
        "/receipt-write-off",
        json={
            "invoices": [
                {"invoice_no": "A", "due_date": "2026-01-01", "receivable": 10}
            ],
            "events": [],
        },
    ).status_code == 422


def test_write_off_non_finite_amount_422():
    for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
        raw = (
            '{"invoices": [{"invoice_no": "A", "due_date": "2026-01-01", '
            '"receivable": 10}], "events": [{"type": "receipt", '
            '"event_id": "R1", "amount": ' + token + '}]}'
        )
        resp = client.post(
            "/receipt-write-off",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422, token

# ---------------------------------------------------------------------------
# /waitlist-trial：限量活动候补席位试算
# ---------------------------------------------------------------------------

_WL_CANDIDATES = [
    {"candidate_id": "c01", "priority": 5, "registered_at_utc": "2026-04-01T09:00:00Z"},
    {"candidate_id": "c02", "priority": 5, "registered_at_utc": "2026-04-01T09:10:00Z"},
    {"candidate_id": "c03", "priority": 7, "registered_at_utc": "2026-04-01T09:50:00Z"},
    {"candidate_id": "c04", "priority": 1, "registered_at_utc": "2026-04-01T08:00:00Z"},
]

_WL_BASE = {
    "seats": 2,
    "invite_ttl_minutes": 30,
    "start_at_utc": "2026-04-01T10:00:00Z",
    "settle_at_utc": "2026-04-01T12:00:00Z",
    "candidates": _WL_CANDIDATES,
    "events": [
        {"type": "confirm", "candidate_id": "c03", "at_utc": "2026-04-01T10:05:00Z"},
        {"type": "cancel", "candidate_id": "c03", "at_utc": "2026-04-01T10:45:00Z"},
        {"type": "confirm", "candidate_id": "c04", "at_utc": "2026-04-01T11:10:00Z"},
    ],
}


def _wl(**overrides):
    payload = {**_WL_BASE, **overrides}
    return client.post("/waitlist-trial", json=payload)


def test_waitlist_trial_full_scenario():
    resp = _wl()
    assert resp.status_code == 200
    body = resp.json()
    # 回显
    assert body["seats"] == 2
    assert body["invite_ttl_minutes"] == 30
    assert body["start_at_utc"] == "2026-04-01T10:00:00Z"
    assert body["settle_at_utc"] == "2026-04-01T12:00:00Z"
    # 候补顺序 c03(p7) > c01(p5,09:00) > c02(p5,09:10) > c04(p1)，起始邀 c03、c01
    assert body["invitation_count"] == 4
    assert body["invitations"] == [
        {
            "candidate_id": "c03",
            "issued_at_utc": "2026-04-01T10:00:00Z",
            "deadline_utc": "2026-04-01T10:30:00Z",
            "result": "confirmed",
            "resolved_at_utc": "2026-04-01T10:05:00Z",
        },
        {
            "candidate_id": "c01",
            "issued_at_utc": "2026-04-01T10:00:00Z",
            "deadline_utc": "2026-04-01T10:30:00Z",
            "result": "expired",
            "resolved_at_utc": "2026-04-01T10:31:00Z",
        },
        {
            "candidate_id": "c02",
            "issued_at_utc": "2026-04-01T10:31:00Z",
            "deadline_utc": "2026-04-01T11:01:00Z",
            "result": "expired",
            "resolved_at_utc": "2026-04-01T11:02:00Z",
        },
        {
            "candidate_id": "c04",
            "issued_at_utc": "2026-04-01T10:45:00Z",
            "deadline_utc": "2026-04-01T11:15:00Z",
            "result": "confirmed",
            "resolved_at_utc": "2026-04-01T11:10:00Z",
        },
    ]
    # 最终状态按候补者输入顺序
    assert body["candidates"] == [
        {"candidate_id": "c01", "status": "expired"},
        {"candidate_id": "c02", "status": "expired"},
        {"candidate_id": "c03", "status": "cancelled"},
        {"candidate_id": "c04", "status": "confirmed"},
    ]


def test_waitlist_trial_no_events_pending_and_waiting():
    resp = _wl(
        seats=1,
        settle_at_utc="2026-04-01T10:20:00Z",
        events=[],
        candidates=[
            {"candidate_id": "a", "priority": 1, "registered_at_utc": "2026-04-01T09:00:00Z"},
            {"candidate_id": "b", "priority": 1, "registered_at_utc": "2026-04-01T09:01:00Z"},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["invitations"] == [
        {
            "candidate_id": "a",
            "issued_at_utc": "2026-04-01T10:00:00Z",
            "deadline_utc": "2026-04-01T10:30:00Z",
            "result": "pending",
            "resolved_at_utc": None,
        }
    ]
    assert body["candidates"] == [
        {"candidate_id": "a", "status": "invited"},
        {"candidate_id": "b", "status": "waiting"},
    ]


def test_waitlist_trial_confirm_on_deadline_minute_ok():
    resp = _wl(
        seats=1,
        candidates=[
            {"candidate_id": "a", "priority": 1, "registered_at_utc": "2026-04-01T09:00:00Z"}
        ],
        events=[{"type": "confirm", "candidate_id": "a", "at_utc": "2026-04-01T10:30:00Z"}],
    )
    assert resp.status_code == 200
    assert resp.json()["candidates"] == [{"candidate_id": "a", "status": "confirmed"}]


def test_waitlist_trial_confirm_after_expiry_409():
    # 10:31 自动失效先于同刻事件：确认无有效邀请，整批拒绝
    resp = _wl(
        seats=1,
        candidates=[
            {"candidate_id": "a", "priority": 1, "registered_at_utc": "2026-04-01T09:00:00Z"},
            {"candidate_id": "b", "priority": 1, "registered_at_utc": "2026-04-01T09:01:00Z"},
        ],
        events=[{"type": "confirm", "candidate_id": "a", "at_utc": "2026-04-01T10:31:00Z"}],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "no_valid_invitation",
        "candidate_id": "a",
        "event_index": 0,
        "at_utc": "2026-04-01T10:31:00Z",
        "reason": "already_exited",
    }


def test_waitlist_trial_confirm_waiting_or_unknown_409():
    # 待邀者确认
    resp = _wl(
        seats=1,
        events=[{"type": "confirm", "candidate_id": "c01", "at_utc": "2026-04-01T10:05:00Z"}],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_invited"
    # 未知编号确认
    resp = _wl(
        seats=1,
        events=[{"type": "confirm", "candidate_id": "ghost", "at_utc": "2026-04-01T10:05:00Z"}],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "not_found"


def test_waitlist_trial_double_confirm_409():
    resp = _wl(
        seats=2,
        events=[
            {"type": "confirm", "candidate_id": "c03", "at_utc": "2026-04-01T10:05:00Z"},
            {"type": "confirm", "candidate_id": "c03", "at_utc": "2026-04-01T10:06:00Z"},
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "no_valid_invitation"
    assert detail["reason"] == "already_confirmed"
    assert detail["event_index"] == 1


def test_waitlist_trial_cancel_unknown_or_exited_409():
    # 未知编号取消
    resp = _wl(
        events=[{"type": "cancel", "candidate_id": "ghost", "at_utc": "2026-04-01T10:05:00Z"}]
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "cancel_target_invalid",
        "candidate_id": "ghost",
        "event_index": 0,
        "at_utc": "2026-04-01T10:05:00Z",
        "reason": "not_found",
    }
    # 重复取消同一对象（已退出）
    resp = _wl(
        events=[
            {"type": "cancel", "candidate_id": "c01", "at_utc": "2026-04-01T10:05:00Z"},
            {"type": "cancel", "candidate_id": "c01", "at_utc": "2026-04-01T10:06:00Z"},
        ]
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "cancel_target_invalid"
    assert detail["reason"] == "already_exited"
    assert detail["event_index"] == 1


def test_waitlist_trial_rejection_is_atomic_no_partial_result():
    # 前一个事件已生效（c03 确认），后一个事件非法 -> 整批拒绝，只含 detail
    resp = _wl(
        events=[
            {"type": "confirm", "candidate_id": "c03", "at_utc": "2026-04-01T10:05:00Z"},
            {"type": "cancel", "candidate_id": "ghost", "at_utc": "2026-04-01T10:06:00Z"},
        ]
    )
    assert resp.status_code == 409
    body = resp.json()
    assert set(body.keys()) == {"detail"}


def test_waitlist_trial_422_validation():
    # 非正席位数 / 保留时长
    assert _wl(seats=0).status_code == 422
    assert _wl(seats=-1).status_code == 422
    assert _wl(invite_ttl_minutes=0).status_code == 422
    # 编号重复
    assert _wl(candidates=_WL_CANDIDATES + [_WL_CANDIDATES[0]]).status_code == 422
    # 空编号 / 空白编号
    assert _wl(candidates=[{"candidate_id": "  ", "priority": 1,
                            "registered_at_utc": "2026-04-01T09:00:00Z"}]).status_code == 422
    # 候补者列表为空
    assert _wl(candidates=[]).status_code == 422
    # 报名晚于起始时刻
    assert _wl(candidates=[{"candidate_id": "a", "priority": 1,
                            "registered_at_utc": "2026-04-01T10:00:01Z"}]).status_code == 422
    # 结算早于起始
    assert _wl(settle_at_utc="2026-04-01T09:59:00Z").status_code == 422
    # 事件越界（早于起始 / 晚于结算）
    assert _wl(events=[{"type": "confirm", "candidate_id": "c03",
                        "at_utc": "2026-04-01T09:59:00Z"}]).status_code == 422
    assert _wl(events=[{"type": "confirm", "candidate_id": "c03",
                        "at_utc": "2026-04-01T12:01:00Z"}]).status_code == 422
    # 事件倒序
    assert _wl(events=[
        {"type": "confirm", "candidate_id": "c03", "at_utc": "2026-04-01T10:05:00Z"},
        {"type": "cancel", "candidate_id": "c01", "at_utc": "2026-04-01T10:04:00Z"},
    ]).status_code == 422
    # 非整分钟时刻
    assert _wl(start_at_utc="2026-04-01T10:00:30Z").status_code == 422
    assert _wl(events=[{"type": "confirm", "candidate_id": "c03",
                        "at_utc": "2026-04-01T10:05:30Z"}]).status_code == 422
    # 非 Z 结尾时刻
    assert _wl(start_at_utc="2026-04-01T10:00:00+08:00").status_code == 422
    # 非法事件类型
    assert _wl(events=[{"type": "noop", "candidate_id": "c03",
                        "at_utc": "2026-04-01T10:05:00Z"}]).status_code == 422
    # 缺字段 / 空请求体
    assert client.post("/waitlist-trial", json={}).status_code == 422
    assert client.post("/waitlist-trial", json={"seats": 1}).status_code == 422


def test_waitlist_trial_registered_at_equal_start_ok():
    resp = _wl(
        seats=1,
        settle_at_utc="2026-04-01T10:20:00Z",
        candidates=[{"candidate_id": "a", "priority": 1,
                     "registered_at_utc": "2026-04-01T10:00:00Z"}],
        events=[],
    )
    assert resp.status_code == 200
    assert resp.json()["candidates"] == [{"candidate_id": "a", "status": "invited"}]
