"""Pydantic 请求/响应模型。

所有校验失败（非法时区、逆序区间、超过 366 天的区间、非法星期等）
都会由 FastAPI 统一转换为 HTTP 422。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_RANGE_DAYS = 366


def _check_iana_timezone(v: str) -> str:
    """校验 IANA 时区名，非法时抛 ValueError（由 FastAPI 转为 422）。"""
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        raise ValueError(f"unknown IANA timezone: {v!r}") from None
    return v


class GapStrategy(str, Enum):
    """本地时刻不存在（春跳）时的策略。"""

    skip = "skip"  # 跳过该次
    shift_forward = "shift_forward"  # 按跳变差值前移


class FoldStrategy(str, Enum):
    """本地时刻重复（秋回）时的策略。"""

    first = "first"  # 取第一次出现（UTC 较早）
    second = "second"  # 取第二次出现（UTC 较晚）
    both = "both"  # 两次都保留


# 0=周一 ... 6=周日（与 datetime.date.weekday() 一致）
Weekday = Annotated[int, Field(ge=0, le=6)]


class ExpandRequest(BaseModel):
    timezone: str = Field(..., description="IANA 时区名，如 Europe/Berlin")
    start_date: date = Field(..., description="区间起始日期（含）")
    end_date: date = Field(..., description="区间结束日期（含）")
    weekdays: set[Weekday] = Field(
        ..., min_length=1, description="星期集合，0=周一 ... 6=周日"
    )
    local_time: time = Field(..., description="本地时刻，如 02:30")
    gap_strategy: GapStrategy = Field(
        default=GapStrategy.skip, description="不存在时刻策略：skip / shift_forward"
    )
    fold_strategy: FoldStrategy = Field(
        default=FoldStrategy.first, description="重复时刻策略：first / second / both"
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        return _check_iana_timezone(v)

    @model_validator(mode="after")
    def _validate_range(self) -> "ExpandRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > MAX_RANGE_DAYS:
            raise ValueError(
                f"date range must not exceed {MAX_RANGE_DAYS} days"
            )
        return self


class InstanceOut(BaseModel):
    local: str = Field(..., description="带偏移的本地时间，ISO 8601")
    offset: str = Field(..., description="UTC 偏移，如 +02:00")
    utc: str = Field(..., description="对应的 UTC 时间，ISO 8601（Z 结尾）")
    reason: str = Field(
        ...,
        description=(
            "决策原因：normal / gap_shifted_forward / fold_first / fold_second"
        ),
    )


class ExpandResponse(BaseModel):
    timezone: str
    count: int = Field(..., description="生成的实例数")
    skipped_count: int = Field(..., description="因 skip 策略被跳过的次数")
    skipped_dates: list[str] = Field(..., description="被跳过实例的本地日期")
    instances: list[InstanceOut]


class OverlapSide(ExpandRequest):
    """一侧日程：完整的展开参数 + 每次实例时长。

    继承 ExpandRequest 的全部校验（合法时区、顺序区间、不超过 366 天等）。
    """

    duration_minutes: int = Field(
        ..., ge=1, le=1440, description="每次实例的时长（分钟），1～1440"
    )


class OverlapRequest(BaseModel):
    a: OverlapSide = Field(..., description="A 侧日程")
    b: OverlapSide = Field(..., description="B 侧日程")


class OverlapOut(BaseModel):
    a_start_utc: str = Field(..., description="A 侧实例起点，ISO 8601（Z 结尾）")
    b_start_utc: str = Field(..., description="B 侧实例起点，ISO 8601（Z 结尾）")
    overlap_start_utc: str = Field(..., description="重叠区间起点（含），UTC")
    overlap_end_utc: str = Field(..., description="重叠区间终点（不含），UTC")
    minutes: float = Field(..., description="重叠时长（分钟）")


class OverlapResponse(BaseModel):
    count: int = Field(..., description="跨侧重叠次数")
    overlaps: list[OverlapOut] = Field(
        ..., description="全部跨侧重叠，按 overlap_start_utc 排序"
    )


class DailyOccupancyRequest(OverlapSide):
    """逐日占用统计请求：与 /overlaps 单侧相同的日程 + 报告时区。

    继承 OverlapSide 的全部校验（合法时区、顺序区间、不超过 366 天、
    duration_minutes 1～1440 等）。
    """

    report_timezone: str = Field(
        ..., description="报告时区（IANA 名），按该时区的本地日界切分"
    )

    @field_validator("report_timezone")
    @classmethod
    def _validate_report_timezone(cls, v: str) -> str:
        return _check_iana_timezone(v)


class DayOccupancyOut(BaseModel):
    date: str = Field(..., description="报告时区的本地日期，YYYY-MM-DD")
    minutes: float = Field(..., description="当日占用的真实分钟数")


class DailyOccupancyResponse(BaseModel):
    report_timezone: str = Field(..., description="报告时区")
    count: int = Field(..., description="有占用的天数")
    days: list[DayOccupancyOut] = Field(
        ..., description="逐日占用分钟数，按日期升序；无实例时为空列表"
    )


class StaffingEmployee(BaseModel):
    """一名员工：唯一 ID + 可为空的日程列表（沿用 /overlaps 单侧参数与校验）。"""

    id: str = Field(..., min_length=1, description="员工唯一 ID")
    schedules: list[OverlapSide] | None = Field(
        default=None,
        description="日程列表（与 /overlaps 单侧相同）；null 或空列表表示无排班",
    )


class StaffingGapsRequest(BaseModel):
    window_start_utc: datetime = Field(
        ..., description="查询窗口起点（含），UTC，ISO 8601 且以 Z 结尾"
    )
    window_end_utc: datetime = Field(
        ..., description="查询窗口终点（不含），UTC，ISO 8601 且以 Z 结尾"
    )
    min_staff: int = Field(..., ge=1, description="最低在岗人数，1～员工总数")
    employees: list[StaffingEmployee] = Field(
        ..., min_length=1, max_length=20, description="1～20 名员工，ID 唯一"
    )

    @field_validator("window_start_utc", "window_end_utc", mode="before")
    @classmethod
    def _require_z_suffix(cls, v: object) -> object:
        """窗口起止必须是以 Z 结尾的 UTC ISO 8601 字符串。"""
        if isinstance(v, str) and not v.endswith("Z"):
            raise ValueError("must be an ISO 8601 UTC timestamp ending with 'Z'")
        return v

    @field_validator("window_start_utc", "window_end_utc")
    @classmethod
    def _normalize_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must be a timezone-aware UTC timestamp ending with 'Z'")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window_and_staffing(self) -> "StaffingGapsRequest":
        if not self.window_start_utc < self.window_end_utc:
            raise ValueError("window_start_utc must be before window_end_utc")
        if self.window_end_utc - self.window_start_utc > timedelta(
            days=MAX_RANGE_DAYS
        ):
            raise ValueError(f"window must not exceed {MAX_RANGE_DAYS} days")
        if self.min_staff > len(self.employees):
            raise ValueError("min_staff must be between 1 and the number of employees")
        ids = [e.id for e in self.employees]
        if len(set(ids)) != len(ids):
            raise ValueError("employee ids must be unique")
        return self


class StaffingGapOut(BaseModel):
    start_utc: str = Field(..., description="缺员区间起点（含），UTC，Z 结尾")
    end_utc: str = Field(..., description="缺员区间终点（不含），UTC，Z 结尾")
    on_duty: list[str] = Field(..., description="区间内在岗员工 ID，升序")
    missing: int = Field(..., description="缺少人数（min_staff - 在岗人数）")


class StaffingGapsResponse(BaseModel):
    count: int = Field(..., description="缺员区间数")
    gaps: list[StaffingGapOut] = Field(
        ..., description="人数不足的非空区间，按起点排序；达标时为空列表"
    )


class RestViolationsRequest(BaseModel):
    """同一员工的一组日程（沿用 /overlaps 单侧参数与校验）+ 最低休息分钟数。"""

    schedules: list[OverlapSide] = Field(
        ..., min_length=1, description="日程列表（与 /overlaps 单侧相同），至少 1 条"
    )
    min_rest_minutes: int = Field(
        ..., ge=1, description="相邻工作段之间的最低休息分钟数"
    )


class RestViolationOut(BaseModel):
    start_utc: str = Field(
        ..., description="空档起点（含，即上一工作段终点），UTC，Z 结尾"
    )
    end_utc: str = Field(
        ..., description="空档终点（不含，即下一工作段起点），UTC，Z 结尾"
    )
    rest_minutes: float = Field(..., description="实际休息分钟数（真实 UTC 流逝）")
    shortfall_minutes: float = Field(
        ..., description="休息差额（min_rest_minutes - rest_minutes）"
    )


class RestViolationsResponse(BaseModel):
    min_rest_minutes: int = Field(..., description="最低休息分钟数阈值")
    count: int = Field(..., description="休息不足的空档数")
    violations: list[RestViolationOut] = Field(
        ..., description="低于阈值的空档，按起点排序；均达标时为空列表"
    )


MAX_FREE_SLOT_WINDOW_DAYS = 31


class FreeSlotsParticipant(BaseModel):
    """一名参与者：可为空的日程列表（沿用 /overlaps 单侧参数与校验）。"""

    schedules: list[OverlapSide] | None = Field(
        default=None,
        description="日程列表（与 /overlaps 单侧相同）；null 或空列表表示全程空闲",
    )


class CommonFreeSlotsRequest(BaseModel):
    window_start_utc: datetime = Field(
        ..., description="查询窗口起点（含），UTC，ISO 8601 且以 Z 结尾"
    )
    window_end_utc: datetime = Field(
        ..., description="查询窗口终点（不含），UTC，ISO 8601 且以 Z 结尾"
    )
    duration_minutes: int = Field(
        ..., ge=1, le=1440, description="预约时长（分钟），1～1440"
    )
    step_minutes: int = Field(
        ..., ge=1, le=1440, description="网格步长（分钟），1～1440"
    )
    participants: list[FreeSlotsParticipant] = Field(
        ..., min_length=1, max_length=10, description="1～10 名参与者"
    )

    @field_validator("window_start_utc", "window_end_utc", mode="before")
    @classmethod
    def _require_z_suffix(cls, v: object) -> object:
        """窗口起止必须是以 Z 结尾的 UTC ISO 8601 字符串。"""
        if isinstance(v, str) and not v.endswith("Z"):
            raise ValueError("must be an ISO 8601 UTC timestamp ending with 'Z'")
        return v

    @field_validator("window_start_utc", "window_end_utc")
    @classmethod
    def _normalize_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must be a timezone-aware UTC timestamp ending with 'Z'")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window(self) -> "CommonFreeSlotsRequest":
        if not self.window_start_utc < self.window_end_utc:
            raise ValueError("window_start_utc must be before window_end_utc")
        if self.window_end_utc - self.window_start_utc > timedelta(
            days=MAX_FREE_SLOT_WINDOW_DAYS
        ):
            raise ValueError(
                f"window must not exceed {MAX_FREE_SLOT_WINDOW_DAYS} days"
            )
        return self


class FreeSlotOut(BaseModel):
    start_utc: str = Field(..., description="预约槽起点（含），UTC，Z 结尾")
    end_utc: str = Field(..., description="预约槽终点（不含），UTC，Z 结尾")


class CommonFreeSlotsResponse(BaseModel):
    count: int = Field(..., description="可用预约槽数")
    slots: list[FreeSlotOut] = Field(
        ..., description="能完整容纳预约的网格起点及对应终点，按起点 UTC 升序"
    )


MAX_SCHEDULE_CHANGE_WINDOW_DAYS = 31


class ScheduleChangesRequest(BaseModel):
    """同一员工变更前后两版日程（均可为空）+ UTC 查询窗口。"""

    window_start_utc: datetime = Field(
        ..., description="查询窗口起点（含），UTC，ISO 8601 且以 Z 结尾"
    )
    window_end_utc: datetime = Field(
        ..., description="查询窗口终点（不含），UTC，ISO 8601 且以 Z 结尾"
    )
    before: list[OverlapSide] | None = Field(
        default=None,
        description="变更前日程列表（与 /overlaps 单侧相同）；null 或空列表表示变更前无排班",
    )
    after: list[OverlapSide] | None = Field(
        default=None,
        description="变更后日程列表（与 /overlaps 单侧相同）；null 或空列表表示变更后无排班",
    )

    @field_validator("window_start_utc", "window_end_utc", mode="before")
    @classmethod
    def _require_z_suffix(cls, v: object) -> object:
        """窗口起止必须是以 Z 结尾的 UTC ISO 8601 字符串。"""
        if isinstance(v, str) and not v.endswith("Z"):
            raise ValueError("must be an ISO 8601 UTC timestamp ending with 'Z'")
        return v

    @field_validator("window_start_utc", "window_end_utc")
    @classmethod
    def _normalize_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must be a timezone-aware UTC timestamp ending with 'Z'")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window(self) -> "ScheduleChangesRequest":
        if not self.window_start_utc < self.window_end_utc:
            raise ValueError("window_start_utc must be before window_end_utc")
        if self.window_end_utc - self.window_start_utc > timedelta(
            days=MAX_SCHEDULE_CHANGE_WINDOW_DAYS
        ):
            raise ValueError(
                f"window must not exceed {MAX_SCHEDULE_CHANGE_WINDOW_DAYS} days"
            )
        return self


class ScheduleChangeOut(BaseModel):
    start_utc: str = Field(..., description="差异区间起点（含），UTC，Z 结尾")
    end_utc: str = Field(..., description="差异区间终点（不含），UTC，Z 结尾")
    minutes: float = Field(..., description="区间真实分钟数（UTC 流逝）")


class ScheduleChangesResponse(BaseModel):
    cancelled_count: int = Field(..., description="取消区间数")
    cancelled_total_minutes: float = Field(..., description="取消总真实分钟数")
    cancelled: list[ScheduleChangeOut] = Field(
        ..., description="取消的最大连续区间，按起点排序；无取消时为空列表"
    )
    added_count: int = Field(..., description="新增区间数")
    added_total_minutes: float = Field(..., description="新增总真实分钟数")
    added: list[ScheduleChangeOut] = Field(
        ..., description="新增的最大连续区间，按起点排序；无新增时为空列表"
    )


MAX_HANDOFF_WINDOW_DAYS = 31


class HandoffRouteRequest(BaseModel):
    """消息在员工间经共同在岗交接传递到目标员工的路径请求。"""

    message_at_utc: datetime = Field(
        ..., description="消息产生时刻（初始持有人此时获得消息），UTC，Z 结尾"
    )
    deadline_utc: datetime = Field(
        ..., description="到达截止时刻（交接终点须不晚于此），UTC，Z 结尾"
    )
    duration_minutes: int = Field(
        ..., ge=1, le=1440, description="一次交接所需时长（分钟），1～1440"
    )
    holder_id: str = Field(..., min_length=1, description="初始持有人唯一 ID")
    target_id: str = Field(..., min_length=1, description="目标员工唯一 ID")
    employees: list[StaffingEmployee] = Field(
        ..., min_length=1, max_length=20, description="1～20 名员工，ID 唯一"
    )

    @field_validator("message_at_utc", "deadline_utc", mode="before")
    @classmethod
    def _require_z_suffix(cls, v: object) -> object:
        """两个时刻必须是以 Z 结尾的 UTC ISO 8601 字符串。"""
        if isinstance(v, str) and not v.endswith("Z"):
            raise ValueError("must be an ISO 8601 UTC timestamp ending with 'Z'")
        return v

    @field_validator("message_at_utc", "deadline_utc")
    @classmethod
    def _normalize_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must be a timezone-aware UTC timestamp ending with 'Z'")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _validate_window_and_refs(self) -> "HandoffRouteRequest":
        if not self.message_at_utc < self.deadline_utc:
            raise ValueError("message_at_utc must be before deadline_utc")
        if self.deadline_utc - self.message_at_utc > timedelta(
            days=MAX_HANDOFF_WINDOW_DAYS
        ):
            raise ValueError(
                f"window must not exceed {MAX_HANDOFF_WINDOW_DAYS} days"
            )
        ids = [e.id for e in self.employees]
        if len(set(ids)) != len(ids):
            raise ValueError("employee ids must be unique")
        referenced = {self.holder_id, self.target_id}
        if not referenced <= set(ids):
            raise ValueError("holder_id and target_id must reference existing employees")
        return self


class HandoffLegOut(BaseModel):
    sender_id: str = Field(..., description="交接发送者（消息当前持有人）ID")
    receiver_id: str = Field(..., description="交接接收者 ID")
    start_utc: str = Field(..., description="交接开始时刻（含），UTC，Z 结尾")
    end_utc: str = Field(
        ..., description="交接结束时刻（不含），接收者此时获得消息，UTC，Z 结尾"
    )


class HandoffRouteResponse(BaseModel):
    arrival_utc: str | None = Field(
        ..., description="消息到达目标的最早时刻，UTC，Z 结尾；无解时为 null"
    )
    count: int = Field(..., description="交接链长度（交接次数）")
    chain: list[HandoffLegOut] = Field(
        ..., description="依次进行的交接（双方 ID 与 UTC 起止）；无解或初始即目标时为空"
    )


# ---------------------------------------------------------------------------
# 库存批次 FEFO 分配
# ---------------------------------------------------------------------------


class BatchIn(BaseModel):
    batch_no: str = Field(..., min_length=1, description="批次号，非空，请求内唯一")
    available_quantity: Decimal = Field(
        ..., description="可用数量，必须为正数；整数或十进制小数（如 12、2.5）"
    )
    expiry_date: str = Field(
        ..., description="失效日期，严格 YYYY-MM-DD（ISO 8601）"
    )

    @field_validator("batch_no")
    @classmethod
    def _strip_batch_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("batch_no must not be empty or whitespace")
        return v

    @field_validator("available_quantity")
    @classmethod
    def _positive_finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("available_quantity must be a finite number")
        if v <= 0:
            raise ValueError("available_quantity must be greater than 0")
        return v

    @field_validator("expiry_date")
    @classmethod
    def _strict_date(cls, v: str) -> str:
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"invalid expiry_date {v!r}: expected YYYY-MM-DD"
            ) from None
        # date.fromisoformat 在新版本会接受部分宽松写法，严格只允许 YYYY-MM-DD
        if parsed.isoformat() != v:
            raise ValueError(
                f"invalid expiry_date {v!r}: expected YYYY-MM-DD"
            )
        return v


class OrderLineIn(BaseModel):
    order_id: str = Field(..., min_length=1, description="订单号，非空，请求内唯一")
    quantity: Decimal = Field(
        ..., description="订单需求数量，必须为正数；整数或十进制小数"
    )

    @field_validator("order_id")
    @classmethod
    def _strip_order_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("order_id must not be empty or whitespace")
        return v

    @field_validator("quantity")
    @classmethod
    def _positive_finite(cls, v: Decimal) -> Decimal:
        if not v.is_finite():
            raise ValueError("quantity must be a finite number")
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        return v


class AllocateInventoryRequest(BaseModel):
    batches: list[BatchIn] = Field(
        ..., min_length=1, description="库存批次，至少 1 个；批次号不得重复"
    )
    orders: list[OrderLineIn] = Field(
        ..., min_length=1, description="订单明细，至少 1 条；订单号不得重复"
    )

    @model_validator(mode="after")
    def _unique_ids(self) -> "AllocateInventoryRequest":
        batch_ids = [b.batch_no.strip() for b in self.batches]
        if len(set(batch_ids)) != len(batch_ids):
            raise ValueError("batch_no values must be unique")
        order_ids = [o.order_id.strip() for o in self.orders]
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("order_id values must be unique")
        return self


def _quantity_number(v: Decimal) -> int | float:
    """响应中的 Decimal 数量：整数值输出 int，否则输出 float。"""
    return int(v) if v == v.to_integral_value() else float(v)


class AllocationLineOut(BaseModel):
    batch_no: str
    expiry_date: str
    quantity: int | float

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class OrderAllocationOut(BaseModel):
    order_id: str
    quantity: int | float = Field(..., description="订单原始需求数量")
    lines: list[AllocationLineOut] = Field(
        ..., description="按 FEFO 取用顺序排列的分配明细（可能跨多个批次）"
    )

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class BatchRemainingOut(BaseModel):
    batch_no: str
    expiry_date: str
    available_before: int | float = Field(..., description="分配前可用量")
    remaining: int | float = Field(..., description="分配后剩余量")

    @field_validator("available_before", "remaining", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class AllocateInventoryResponse(BaseModel):
    allocations: list[OrderAllocationOut] = Field(
        ..., description="每条订单的分配明细，按输入顺序返回"
    )
    remaining_stock: list[BatchRemainingOut] = Field(
        ..., description="各批次分配前/后数量，按失效日期、批次号（FEFO）排序"
    )
    total_allocated: int | float = Field(..., description="总分配量")

    @field_validator("total_allocated", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


# ---------------------------------------------------------------------------
# 收款对发票核销试算
# ---------------------------------------------------------------------------

# 金额允许的最小单位：0.01 元（分）
_CENT = Decimal("0.01")


def _check_money(v: Decimal) -> Decimal:
    """金额必须为有限正数，且至多两位小数（按分精确计算）。"""
    if not v.is_finite():
        raise ValueError("amount must be a finite number")
    if v <= 0:
        raise ValueError("amount must be greater than 0")
    if v.quantize(_CENT) != v:
        raise ValueError("amount must have at most two decimal places")
    return v


class ReceivableInvoiceIn(BaseModel):
    invoice_no: str = Field(..., min_length=1, description="发票号，非空，请求内唯一")
    due_date: str = Field(..., description="到期日，严格 YYYY-MM-DD（ISO 8601）")
    receivable: Decimal = Field(
        ..., description="应收金额，必须为正数且至多两位小数"
    )

    @field_validator("invoice_no")
    @classmethod
    def _strip_invoice_no(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("invoice_no must not be empty or whitespace")
        return v

    @field_validator("due_date")
    @classmethod
    def _strict_date(cls, v: str) -> str:
        try:
            parsed = date.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"invalid due_date {v!r}: expected YYYY-MM-DD"
            ) from None
        if parsed.isoformat() != v:
            raise ValueError(f"invalid due_date {v!r}: expected YYYY-MM-DD")
        return v

    @field_validator("receivable")
    @classmethod
    def _check_receivable(cls, v: Decimal) -> Decimal:
        return _check_money(v)


class ReceiptEventIn(BaseModel):
    """收款事件：按顺序核销发票未结金额。"""

    type: Literal["receipt"] = Field(
        ..., description="事件类型，固定为 receipt（收款）"
    )
    event_id: str = Field(
        ...,
        min_length=1,
        description="事件号，非空；相同事件号视为同一事件的重放",
    )
    amount: Decimal = Field(
        ..., description="收款金额，必须为正数且至多两位小数"
    )

    @field_validator("event_id")
    @classmethod
    def _strip_event_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("event_id must not be empty or whitespace")
        return v

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v: Decimal) -> Decimal:
        return _check_money(v)


class ReversalEventIn(BaseModel):
    """撤回事件：释放先前某笔收款原先核销的金额。"""

    type: Literal["reversal"] = Field(
        ..., description="事件类型，固定为 reversal（撤回）"
    )
    event_id: str = Field(
        ...,
        min_length=1,
        description="事件号，非空；相同事件号视为同一事件的重放",
    )
    reversal_of: str = Field(
        ..., min_length=1, description="被撤回的先前收款事件号"
    )

    @field_validator("event_id", "reversal_of")
    @classmethod
    def _strip_ids(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("event ids must not be empty or whitespace")
        return v


WriteOffEventIn = Annotated[
    ReceiptEventIn | ReversalEventIn,
    Field(
        ...,
        discriminator="type",
        description="按顺序处理的收款（receipt）或撤回（reversal）事件",
    ),
]


class ReceiptWriteOffRequest(BaseModel):
    invoices: list[ReceivableInvoiceIn] = Field(
        ..., min_length=1, description="发票，至少 1 张；发票号不得重复"
    )
    events: list[WriteOffEventIn] = Field(
        ..., min_length=1, description="按顺序排列的收款 / 撤回事件，至少 1 个"
    )

    @model_validator(mode="after")
    def _unique_invoice_ids(self) -> "ReceiptWriteOffRequest":
        # 注意：事件号允许重复——相同事件号代表同一事件的重放，
        # 由核销逻辑判定“内容一致只计一次 / 内容不同整批拒绝”，
        # 模型层不做事件号唯一校验
        invoice_nos = [inv.invoice_no for inv in self.invoices]
        if len(set(invoice_nos)) != len(invoice_nos):
            raise ValueError("invoice_no values must be unique")
        return self


class WriteOffLineOut(BaseModel):
    invoice_no: str
    due_date: str
    amount: int | float

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class ReceiptWriteOffOut(BaseModel):
    event_id: str
    amount: int | float = Field(..., description="收款原始金额")
    lines: list[WriteOffLineOut] = Field(
        ..., description="按到期日、发票号填补顺序排列的核销明细"
    )

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class InvoiceOutstandingOut(BaseModel):
    invoice_no: str
    due_date: str
    receivable: int | float = Field(..., description="发票原始应收金额")
    outstanding: int | float = Field(..., description="试算后未结金额")

    @field_validator("receivable", "outstanding", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v


class ReceiptWriteOffResponse(BaseModel):
    receipts: list[ReceiptWriteOffOut] = Field(
        ...,
        description=(
            "各有效收款（未被撤回）的核销明细，按事件处理顺序返回；"
            "内容一致的重放不重复输出，被撤回的收款不输出"
        ),
    )
    invoices: list[InvoiceOutstandingOut] = Field(
        ..., description="每张发票的未结金额，按到期日、发票号排序"
    )
    total_written_off: int | float = Field(
        ..., description="有效收款的核销总额（全部未结金额减少量）"
    )

    @field_validator("total_written_off", mode="before")
    @classmethod
    def _coerce(cls, v: object) -> object:
        return _quantity_number(v) if isinstance(v, Decimal) else v
