"""FastAPI 入口：民用日程展开服务。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .inventory import BatchInput, InsufficientStock, OrderInput, allocate
from .models import (
    AllocateInventoryRequest,
    AllocateInventoryResponse,
    AllocationLineOut,
    BatchRemainingOut,
    CommonFreeSlotsRequest,
    CommonFreeSlotsResponse,
    DailyOccupancyRequest,
    DailyOccupancyResponse,
    ExpandRequest,
    ExpandResponse,
    FreeSlotOut,
    HandoffLegOut,
    HandoffRouteRequest,
    HandoffRouteResponse,
    InvoiceOutstandingOut,
    OrderAllocationOut,
    OverlapRequest,
    OverlapResponse,
    ReceiptEventIn,
    ReceiptWriteOffRequest,
    ReceiptWriteOffResponse,
    ReceiptWriteOffOut,
    ReversalEventIn,
    RestViolationOut,
    RestViolationsRequest,
    RestViolationsResponse,
    ScheduleChangeOut,
    ScheduleChangesRequest,
    ScheduleChangesResponse,
    StaffingGapOut,
    StaffingGapsRequest,
    StaffingGapsResponse,
    WaitlistCandidateOut,
    WaitlistInvitationOut,
    WaitlistTrialRequest,
    WaitlistTrialResponse,
    WriteOffLineOut,
)
from .receivables import (
    Event,
    InvoiceInput,
    WriteOffRejected,
    simulate as simulate_write_off,
)
from .waitlist import (
    CandidateInput,
    TrialEvent,
    WaitlistRejected,
    simulate as simulate_waitlist,
)
from .scheduler import (
    common_free_slots,
    expand_schedule,
    find_overlaps,
    handoff_route,
    instance_payload,
    occupancy_by_day,
    rest_violations,
    schedule_changes,
    staffing_gaps,
    utc_iso,
)

app = FastAPI(
    title="Civil Schedule Expander",
    version="1.2.0",
    description=(
        "基于标准库 zoneinfo 的民用日程展开 API："
        "通过 UTC 往返校验区分正常 / 跳时 / 叠时，"
        "按策略生成去重且按 UTC 稳定排序的实例，"
        "并可检测两个日程在真实时间轴上的重叠、按报告时区统计逐日占用，"
        "按 UTC 窗口扫描多员工在岗人数缺口，"
        "检查同一员工相邻工作段之间的休息是否低于最低分钟数，"
        "以及在 UTC 窗口内按步长网格枚举多名参与者共同空闲的预约槽，"
        "并对账同一员工变更前后两版日程在 UTC 窗口内的取消与新增区间，"
        "以及在多名员工的连续共同在岗期间规划消息交接的最早到达链。"
        "另提供 /inventory-allocation 仓储 FEFO 批次分配，"
        "以及 /receipt-write-off 收款对发票的按序核销试算："
        "收款按到期日、发票号顺序填补未结金额、可拆到多张发票，"
        "撤回仅释放原收款核销额且不重排其他收款，支持幂等重放，"
        "任何业务冲突均整批拒绝、不返回部分试算。"
        "另有 /waitlist-trial 限量活动候补席位试算："
        "按优先级、报名时刻、编号顺序邀请至满额，邀请保留至截止当分钟、"
        "下一分钟自动失效并立即补位，确认 / 取消事件按时刻顺序处理，"
        "任何非法确认或取消均整批拒绝、不返回部分试算。"
    ),
)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic 拒绝 NaN/Infinity 时会把非有限浮点原样放进 error["input"]，
    # json.dumps 默认无法序列化它们，这里替换为字符串以保证 422 正常返回
    detail = exc.errors()
    for err in detail:
        value = err.get("input")
        if isinstance(value, float) and not math.isfinite(value):
            err["input"] = str(value)
    return JSONResponse(
        status_code=422, content={"detail": jsonable_encoder(detail)}
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post("/expand", response_model=ExpandResponse, summary="展开日程")
def expand(req: ExpandRequest) -> ExpandResponse:
    tz = ZoneInfo(req.timezone)
    instances, skipped = expand_schedule(
        tz=tz,
        start_date=req.start_date,
        end_date=req.end_date,
        weekdays=req.weekdays,
        local_time=req.local_time,
        gap_strategy=req.gap_strategy.value,
        fold_strategy=req.fold_strategy.value,
    )
    return ExpandResponse(
        timezone=req.timezone,
        count=len(instances),
        skipped_count=len(skipped),
        skipped_dates=[d.isoformat() for d in skipped],
        instances=[instance_payload(i, tz) for i in instances],
    )


@app.post("/overlaps", response_model=OverlapResponse, summary="检测两个日程的重叠")
def overlaps(req: OverlapRequest) -> OverlapResponse:
    # 两侧各自按原 gap/fold 策略展开（实例已按 UTC 去重、排序）
    expanded = []
    for side in (req.a, req.b):
        tz = ZoneInfo(side.timezone)
        instances, _ = expand_schedule(
            tz=tz,
            start_date=side.start_date,
            end_date=side.end_date,
            weekdays=side.weekdays,
            local_time=side.local_time,
            gap_strategy=side.gap_strategy.value,
            fold_strategy=side.fold_strategy.value,
        )
        expanded.append(instances)
    found = find_overlaps(
        a_instances=expanded[0],
        a_duration_minutes=req.a.duration_minutes,
        b_instances=expanded[1],
        b_duration_minutes=req.b.duration_minutes,
    )
    return OverlapResponse(
        count=len(found),
        overlaps=[
            {
                "a_start_utc": utc_iso(o.a_start),
                "b_start_utc": utc_iso(o.b_start),
                "overlap_start_utc": utc_iso(o.start),
                "overlap_end_utc": utc_iso(o.end),
                "minutes": o.minutes,
            }
            for o in found
        ],
    )


@app.post(
    "/daily-occupancy",
    response_model=DailyOccupancyResponse,
    summary="按报告时区统计逐日占用分钟数",
)
def daily_occupancy(req: DailyOccupancyRequest) -> DailyOccupancyResponse:
    tz = ZoneInfo(req.timezone)
    instances, _ = expand_schedule(
        tz=tz,
        start_date=req.start_date,
        end_date=req.end_date,
        weekdays=req.weekdays,
        local_time=req.local_time,
        gap_strategy=req.gap_strategy.value,
        fold_strategy=req.fold_strategy.value,
    )
    days = occupancy_by_day(
        instances=instances,
        duration_minutes=req.duration_minutes,
        report_tz=ZoneInfo(req.report_timezone),
    )
    return DailyOccupancyResponse(
        report_timezone=req.report_timezone,
        count=len(days),
        days=[{"date": d.isoformat(), "minutes": m} for d, m in days],
    )


@app.post(
    "/staffing-gaps",
    response_model=StaffingGapsResponse,
    summary="查询最低在岗人数缺口",
)
def staffing_gaps_check(req: StaffingGapsRequest) -> StaffingGapsResponse:
    # 每名员工的各条日程按原 gap/fold 策略展开，实例按真实时长转为半开 UTC 区间
    employee_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for emp in req.employees:
        intervals: list[tuple[datetime, datetime]] = []
        for side in emp.schedules or []:
            tz = ZoneInfo(side.timezone)
            instances, _ = expand_schedule(
                tz=tz,
                start_date=side.start_date,
                end_date=side.end_date,
                weekdays=side.weekdays,
                local_time=side.local_time,
                gap_strategy=side.gap_strategy.value,
                fold_strategy=side.fold_strategy.value,
            )
            span = timedelta(minutes=side.duration_minutes)
            intervals.extend((i.utc, i.utc + span) for i in instances)
        employee_intervals[emp.id] = intervals
    gaps = staffing_gaps(
        window_start=req.window_start_utc,
        window_end=req.window_end_utc,
        min_staff=req.min_staff,
        employee_intervals=employee_intervals,
    )
    return StaffingGapsResponse(
        count=len(gaps),
        gaps=[
            StaffingGapOut(
                start_utc=utc_iso(g.start),
                end_utc=utc_iso(g.end),
                on_duty=list(g.on_duty),
                missing=g.missing,
            )
            for g in gaps
        ],
    )


@app.post(
    "/rest-violations",
    response_model=RestViolationsResponse,
    summary="检查相邻工作段间休息不足的空档",
)
def rest_violations_check(req: RestViolationsRequest) -> RestViolationsResponse:
    # 同一员工的各条日程按原 gap/fold 策略展开，实例按真实时长转为半开 UTC 区间
    intervals: list[tuple[datetime, datetime]] = []
    for side in req.schedules:
        tz = ZoneInfo(side.timezone)
        instances, _ = expand_schedule(
            tz=tz,
            start_date=side.start_date,
            end_date=side.end_date,
            weekdays=side.weekdays,
            local_time=side.local_time,
            gap_strategy=side.gap_strategy.value,
            fold_strategy=side.fold_strategy.value,
        )
        span = timedelta(minutes=side.duration_minutes)
        intervals.extend((i.utc, i.utc + span) for i in instances)
    violations = rest_violations(
        intervals=intervals, min_rest_minutes=req.min_rest_minutes
    )
    return RestViolationsResponse(
        min_rest_minutes=req.min_rest_minutes,
        count=len(violations),
        violations=[
            RestViolationOut(
                start_utc=utc_iso(v.start),
                end_utc=utc_iso(v.end),
                rest_minutes=v.rest_minutes,
                shortfall_minutes=req.min_rest_minutes - v.rest_minutes,
            )
            for v in violations
        ],
    )


@app.post(
    "/common-free-slots",
    response_model=CommonFreeSlotsResponse,
    summary="查询多名参与者共同空闲的预约槽",
)
def common_free_slots_check(req: CommonFreeSlotsRequest) -> CommonFreeSlotsResponse:
    # 每名参与者的各条日程按原 gap/fold 策略展开，实例按真实时长转为半开 UTC 区间
    participant_intervals: list[list[tuple[datetime, datetime]]] = []
    for participant in req.participants:
        intervals: list[tuple[datetime, datetime]] = []
        for side in participant.schedules or []:
            tz = ZoneInfo(side.timezone)
            instances, _ = expand_schedule(
                tz=tz,
                start_date=side.start_date,
                end_date=side.end_date,
                weekdays=side.weekdays,
                local_time=side.local_time,
                gap_strategy=side.gap_strategy.value,
                fold_strategy=side.fold_strategy.value,
            )
            span = timedelta(minutes=side.duration_minutes)
            intervals.extend((i.utc, i.utc + span) for i in instances)
        participant_intervals.append(intervals)
    slots = common_free_slots(
        window_start=req.window_start_utc,
        window_end=req.window_end_utc,
        duration_minutes=req.duration_minutes,
        step_minutes=req.step_minutes,
        participant_intervals=participant_intervals,
    )
    return CommonFreeSlotsResponse(
        count=len(slots),
        slots=[
            FreeSlotOut(start_utc=utc_iso(start), end_utc=utc_iso(end))
            for start, end in slots
        ],
    )


@app.post(
    "/schedule-changes",
    response_model=ScheduleChangesResponse,
    summary="对账同一员工变更前后的排班差异",
)
def schedule_changes_check(req: ScheduleChangesRequest) -> ScheduleChangesResponse:
    # 变更前后两版日程各自按原 gap/fold 策略展开，实例按真实时长转为半开 UTC 区间
    versions: list[list[tuple[datetime, datetime]]] = []
    for sides in (req.before, req.after):
        intervals: list[tuple[datetime, datetime]] = []
        for side in sides or []:
            tz = ZoneInfo(side.timezone)
            instances, _ = expand_schedule(
                tz=tz,
                start_date=side.start_date,
                end_date=side.end_date,
                weekdays=side.weekdays,
                local_time=side.local_time,
                gap_strategy=side.gap_strategy.value,
                fold_strategy=side.fold_strategy.value,
            )
            span = timedelta(minutes=side.duration_minutes)
            intervals.extend((i.utc, i.utc + span) for i in instances)
        versions.append(intervals)
    cancelled, added = schedule_changes(
        window_start=req.window_start_utc,
        window_end=req.window_end_utc,
        before_intervals=versions[0],
        after_intervals=versions[1],
    )

    def _payload(
        segments: list[tuple[datetime, datetime]],
    ) -> list[ScheduleChangeOut]:
        return [
            ScheduleChangeOut(
                start_utc=utc_iso(start),
                end_utc=utc_iso(end),
                minutes=(end - start).total_seconds() / 60,
            )
            for start, end in segments
        ]

    cancelled_out = _payload(cancelled)
    added_out = _payload(added)
    return ScheduleChangesResponse(
        cancelled_count=len(cancelled_out),
        cancelled_total_minutes=sum(c.minutes for c in cancelled_out),
        cancelled=cancelled_out,
        added_count=len(added_out),
        added_total_minutes=sum(a.minutes for a in added_out),
        added=added_out,
    )


@app.post(
    "/handoff-route",
    response_model=HandoffRouteResponse,
    summary="规划消息经共同在岗交接到达目标的最早链",
)
def handoff_route_check(req: HandoffRouteRequest) -> HandoffRouteResponse:
    # 每名员工的各条日程按原 gap/fold 策略展开，实例按真实时长转为半开 UTC 区间
    employee_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for emp in req.employees:
        intervals: list[tuple[datetime, datetime]] = []
        for side in emp.schedules or []:
            tz = ZoneInfo(side.timezone)
            instances, _ = expand_schedule(
                tz=tz,
                start_date=side.start_date,
                end_date=side.end_date,
                weekdays=side.weekdays,
                local_time=side.local_time,
                gap_strategy=side.gap_strategy.value,
                fold_strategy=side.fold_strategy.value,
            )
            span = timedelta(minutes=side.duration_minutes)
            intervals.extend((i.utc, i.utc + span) for i in instances)
        employee_intervals[emp.id] = intervals
    arrival, chain = handoff_route(
        message_at=req.message_at_utc,
        deadline=req.deadline_utc,
        holder=req.holder_id,
        target=req.target_id,
        duration_minutes=req.duration_minutes,
        employee_intervals=employee_intervals,
    )
    legs = [
        HandoffLegOut(
            sender_id=leg.sender,
            receiver_id=leg.receiver,
            start_utc=utc_iso(leg.start),
            end_utc=utc_iso(leg.end),
        )
        for leg in chain
    ]
    return HandoffRouteResponse(
        arrival_utc=utc_iso(arrival) if arrival is not None else None,
        count=len(legs),
        chain=legs,
    )


@app.post(
    "/inventory-allocation",
    response_model=AllocateInventoryResponse,
    summary="按 FEFO 把订单数量分配到库存批次",
)
def inventory_allocation(req: AllocateInventoryRequest) -> AllocateInventoryResponse:
    # 数量用 Decimal 精确运算；FEFO 排序与订单满足由纯逻辑的模块完成
    try:
        allocations, remaining_rows, total_allocated = allocate(
            batches=[
                BatchInput(
                    batch_no=b.batch_no,
                    available_quantity=b.available_quantity,
                    expiry_date=date.fromisoformat(b.expiry_date),
                )
                for b in req.batches
            ],
            orders=[
                OrderInput(order_id=o.order_id, quantity=o.quantity)
                for o in req.orders
            ],
        )
    except InsufficientStock as exc:
        # 库存不足：整批拒绝（409），不返回任何部分分配
        return JSONResponse(status_code=409, content={"detail": exc.detail()})
    return AllocateInventoryResponse(
        allocations=[
            OrderAllocationOut(
                order_id=a.order_id,
                quantity=a.quantity,
                lines=[
                    AllocationLineOut(
                        batch_no=line.batch_no,
                        expiry_date=line.expiry_date.isoformat(),
                        quantity=line.quantity,
                    )
                    for line in a.lines
                ],
            )
            for a in allocations
        ],
        remaining_stock=[
            BatchRemainingOut(
                batch_no=row.batch_no,
                expiry_date=row.expiry_date.isoformat(),
                available_before=row.available_before,
                remaining=row.remaining,
            )
            for row in remaining_rows
        ],
        total_allocated=total_allocated,
    )


@app.post(
    "/receipt-write-off",
    response_model=ReceiptWriteOffResponse,
    summary="按顺序试算收款（含撤回）对发票的核销",
)
def receipt_write_off(req: ReceiptWriteOffRequest) -> ReceiptWriteOffResponse | JSONResponse:
    # 金额用 Decimal 精确运算（按分）；核销顺序、撤回释放与重放幂等
    # 全部由纯逻辑模块完成，业务冲突整批拒绝（409）不返回部分试算
    events: list[Event] = []
    for ev in req.events:
        if isinstance(ev, ReceiptEventIn):
            events.append(Event(kind="receipt", event_id=ev.event_id, amount=ev.amount))
        else:
            assert isinstance(ev, ReversalEventIn)
            events.append(
                Event(
                    kind="reversal",
                    event_id=ev.event_id,
                    reversal_of=ev.reversal_of,
                )
            )
    try:
        result = simulate_write_off(
            invoices=[
                InvoiceInput(
                    invoice_no=inv.invoice_no,
                    due_date=date.fromisoformat(inv.due_date),
                    receivable=inv.receivable,
                )
                for inv in req.invoices
            ],
            events=events,
        )
    except WriteOffRejected as exc:
        return JSONResponse(status_code=409, content={"detail": exc.detail()})
    return ReceiptWriteOffResponse(
        receipts=[
            ReceiptWriteOffOut(
                event_id=r.event_id,
                amount=r.amount,
                lines=[
                    WriteOffLineOut(
                        invoice_no=line.invoice_no,
                        due_date=line.due_date.isoformat(),
                        amount=line.amount,
                    )
                    for line in r.lines
                ],
            )
            for r in result.receipts
        ],
        invoices=[
            InvoiceOutstandingOut(
                invoice_no=row.invoice_no,
                due_date=row.due_date.isoformat(),
                receivable=row.receivable,
                outstanding=row.outstanding,
            )
            for row in result.invoices
        ],
        total_written_off=result.total_written_off,
    )


@app.post(
    "/waitlist-trial",
    response_model=WaitlistTrialResponse,
    summary="试算限量活动的候补席位邀请、确认与取消",
)
def waitlist_trial(req: WaitlistTrialRequest) -> WaitlistTrialResponse | JSONResponse:
    # 纯逻辑模块按整分钟 UTC 时间轴模拟：起始邀请至满额、自动失效先于
    # 同刻事件、同刻事件按输入顺序、空位立即补位；业务冲突整批拒绝（409）
    try:
        result = simulate_waitlist(
            seats=req.seats,
            start_at=req.start_at_utc,
            ttl_minutes=req.invite_ttl_minutes,
            settle_at=req.settle_at_utc,
            candidates=[
                CandidateInput(
                    candidate_id=c.candidate_id,
                    priority=c.priority,
                    registered_at=c.registered_at_utc,
                )
                for c in req.candidates
            ],
            events=[
                TrialEvent(
                    kind=ev.type,
                    candidate_id=ev.candidate_id,
                    at=ev.at_utc,
                    index=i,
                )
                for i, ev in enumerate(req.events)
            ],
        )
    except WaitlistRejected as exc:
        return JSONResponse(status_code=409, content={"detail": exc.detail()})
    return WaitlistTrialResponse(
        seats=req.seats,
        invite_ttl_minutes=req.invite_ttl_minutes,
        start_at_utc=utc_iso(req.start_at_utc),
        settle_at_utc=utc_iso(req.settle_at_utc),
        invitation_count=len(result.invitations),
        invitations=[
            WaitlistInvitationOut(
                candidate_id=inv.candidate_id,
                issued_at_utc=utc_iso(inv.issued_at),
                deadline_utc=utc_iso(inv.deadline),
                result=inv.result,
                resolved_at_utc=(
                    utc_iso(inv.resolved_at) if inv.resolved_at is not None else None
                ),
            )
            for inv in result.invitations
        ],
        candidates=[
            WaitlistCandidateOut(candidate_id=o.candidate_id, status=o.status)
            for o in result.candidates
        ],
    )
