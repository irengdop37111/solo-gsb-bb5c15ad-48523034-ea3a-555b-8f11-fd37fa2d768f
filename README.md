# Civil Schedule Expander

基于 **FastAPI + 标准库 zoneinfo** 的民用日程展开 API。输入 IANA 时区、日期区间、
星期集合与本地时刻，服务通过 **UTC 往返校验**区分正常 / 跳时（gap）/ 叠时（fold），
按指定策略生成**去重且按 UTC 稳定排序**的实例，并返回每个实例的本地时间、UTC 偏移、
UTC 时间与决策原因。`POST /overlaps` 在此基础上为两个日程各附加时长，
用**扫描线**检测它们在真实时间轴上的全部冲突；`POST /daily-occupancy`
则把单侧日程的占用区间合并后按**报告时区的本地日界**切分，逐日统计真实占用分钟数；
`POST /staffing-gaps` 在 UTC 窗口内扫描多名员工的上下岗事件，
输出在岗人数低于最低要求的缺员区间；`POST /rest-violations`
把同一员工的多条日程在真实 UTC 时间轴上合并为工作段，
检查相邻工作段之间的休息空档是否低于最低分钟数；
`POST /common-free-slots` 把每名参与者的忙碌区间合并后取窗口内的共同空闲，
按**以窗口起点为原点的步长网格**枚举能完整容纳预约的起止时刻；
`POST /schedule-changes` 对账同一员工**变更前后两版日程**，
在半开 UTC 窗口内比较两版工作时间集合，
输出**取消与新增的最大连续区间**、每段真实分钟数及两类总分钟数；
`POST /handoff-route` 则把每名员工的日程展开为半开 UTC 在岗区间并合并
相交或相接者，在**任意两人的连续共同在岗**约束下，求消息经若干次交接
到达目标员工的**最早时刻与一条交接链**，交接期间发送者须已持有消息，
持有期间离岗不丢失消息。
此外 `POST /inventory-allocation` 提供仓储 FEFO 批次分配：
按失效日期（同日按批次号字典序）把订单数量拆分配到库存批次，
全部订单足量满足才整批提交，库存不足时整批拒绝且不返回部分分配。
`POST /receipt-write-off` 提供商户收款对发票的核销试算：
收款按到期日（同日按发票号字典序）顺序填补未结金额、单笔可拆到多张发票，
撤回只释放目标收款原先核销的金额而不重排其他收款，释放额度可供后续收款使用；
相同事件号的重放按幂等处理（内容一致只计一次，内容不同整批拒绝），
撤回不存在或已撤回的收款、以及收款超过剩余应收同样整批拒绝、不返回部分试算。

> **本轮变动（v1.1.0）**：新增 `POST /receipt-write-off` 收款核销试算接口与
> 纯逻辑模块 `app/receivables.py`（Decimal 按分精确运算、原子整批拒绝）；
> 既有接口与行为保持不变。启动方式、健康检查与镜像构建期自检不变
> （自检额外断言 `/receipt-write-off` 路由已注册）。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖（FastAPI / uvicorn / tzdata）在镜像构建时安装完毕，构建期还会执行一次
应用导入自检（确认全部接口随镜像就绪）；容器启动即自动监听 8000 端口提供服务，
无需额外初始化步骤。

- 服务地址： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- 接口（均为 POST，端口同上）： `/expand`、`/overlaps`、`/daily-occupancy`、`/staffing-gaps`、`/rest-violations`、`/common-free-slots`、`/schedule-changes`、`/handoff-route`、`/inventory-allocation`、`/receipt-write-off`

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` 后访问 http://localhost:9000 |

容器内服务固定监听 **8000 端口**（Compose 将宿主机 `${PORT:-8000}` 映射到容器
8000），无需其他配置；时区数据库由镜像内系统 tzdata 及 pip 包 `tzdata`（兜底）
提供。

## API

### `POST /expand`

请求体：

```json
{
  "timezone": "Europe/Berlin",
  "start_date": "2026-03-28",
  "end_date": "2026-03-30",
  "weekdays": [5, 6, 0],
  "local_time": "02:30",
  "gap_strategy": "shift_forward",
  "fold_strategy": "both"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timezone` | string | IANA 时区名，如 `America/New_York` |
| `start_date` / `end_date` | date | 日期区间（两端均含） |
| `weekdays` | int[] | 星期集合，`0`=周一 … `6`=周日，至少 1 个 |
| `local_time` | string | 本地时刻，如 `02:30` |
| `gap_strategy` | string | 不存在时刻策略：`skip`（默认，跳过）/ `shift_forward`（按跳变差值前移） |
| `fold_strategy` | string | 重复时刻策略：`first`（默认，取第一次）/ `second`（取第二次）/ `both`（两次都保留） |

响应 `200`：

```json
{
  "timezone": "Europe/Berlin",
  "count": 3,
  "skipped_count": 0,
  "skipped_dates": [],
  "instances": [
    {"local": "2026-03-28T02:30:00+01:00", "offset": "+01:00", "utc": "2026-03-28T01:30:00Z", "reason": "normal"},
    {"local": "2026-03-29T03:30:00+02:00", "offset": "+02:00", "utc": "2026-03-29T01:30:00Z", "reason": "gap_shifted_forward"},
    {"local": "2026-03-30T02:30:00+02:00", "offset": "+02:00", "utc": "2026-03-30T00:30:00Z", "reason": "normal"}
  ]
}
```

决策原因（`reason`）：

| 值 | 含义 |
| --- | --- |
| `normal` | 正常时刻，本地时间与 UTC 一一对应 |
| `gap_shifted_forward` | 原时刻不存在（春跳），已按跳变差值前移 |
| `fold_first` | 重复时刻（秋回），取第一次出现（UTC 较早） |
| `fold_second` | 重复时刻（秋回），取第二次出现（UTC 较晚） |

被 `skip` 策略跳过的日期不计入 `instances`，汇总在 `skipped_count` / `skipped_dates`。

### `POST /overlaps`

判断两个民用周期日程在真实时间轴上的冲突。请求体的 `a` / `b` 两侧各自携带
与 `/expand` 相同的展开参数（含各自的 `gap_strategy` / `fold_strategy`），外加
`duration_minutes`；两侧独立展开后，把每个实例解释为**半开 UTC 区间**
`[start, start+duration)`，用扫描线枚举全部跨侧重叠。**边界相接不算冲突**
（一侧终点等于另一侧起点时重叠为空）。

请求体：

```json
{
  "a": {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60
  },
  "b": {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `a` / `b` | object | 一侧日程：全部 `/expand` 请求字段 + `duration_minutes` |
| `duration_minutes` | int | 每次实例的时长（分钟），`1`～`1440` |

响应 `200`（`overlaps` 按 `overlap_start_utc` 排序）：

```json
{
  "count": 1,
  "overlaps": [
    {
      "a_start_utc": "2026-03-02T08:00:00Z",
      "b_start_utc": "2026-03-02T08:30:00Z",
      "overlap_start_utc": "2026-03-02T08:30:00Z",
      "overlap_end_utc": "2026-03-02T09:00:00Z",
      "minutes": 30.0
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `a_start_utc` / `b_start_utc` | 两侧实例起点（UTC，Z 结尾） |
| `overlap_start_utc` / `overlap_end_utc` | 重叠区间起止（半开，UTC） |
| `minutes` | 重叠时长（分钟） |

每侧的日期区间同样限制为不超过 366 天，校验失败返回 422。

### `POST /daily-occupancy`

统计单个日程在报告时区内的逐日占用时长。请求体与 `/overlaps` 单侧相同
（全部 `/expand` 展开参数 + `duration_minutes`），外加 `report_timezone`。
实例按既有策略展开后，每个实例解释为**半开 UTC 区间**
`[start, start+duration)`；先**合并相交或相接的区间**（重复时刻产生的
重叠只计一次），再按报告时区的**本地日界**切分，逐日返回占用的真实分钟数。
跨午夜自然落到相邻两日；夏令时切换日（23 / 25 小时）仍按实际 UTC 流逝累计。
无实例时 `days` 为空列表。

请求体：

```json
{
  "timezone": "America/New_York",
  "start_date": "2026-03-07",
  "end_date": "2026-03-07",
  "weekdays": [5],
  "local_time": "23:00",
  "duration_minutes": 180,
  "report_timezone": "America/New_York"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| （展开参数） | — | 与 `/expand` 请求字段相同，校验规则一致 |
| `duration_minutes` | int | 每次实例的时长（分钟），`1`～`1440` |
| `report_timezone` | string | 报告时区（IANA 名），按该时区的本地日界切分 |

响应 `200`（`days` 按日期升序）：

```json
{
  "report_timezone": "America/New_York",
  "count": 2,
  "days": [
    {"date": "2026-03-07", "minutes": 60.0},
    {"date": "2026-03-08", "minutes": 120.0}
  ]
}
```

上例中实例为纽约 2026-03-07 23:00 EST（= 03-08 04:00 UTC）起 180 分钟，
跨越春跳（03-08 当日仅 23 小时），仍按真实 UTC 时长计入两日。

| 字段 | 说明 |
| --- | --- |
| `report_timezone` | 报告时区 |
| `count` | 有占用的天数 |
| `days[].date` | 报告时区的本地日期（YYYY-MM-DD） |
| `days[].minutes` | 当日占用的真实分钟数 |

### `POST /staffing-gaps`

在 UTC 窗口内查询在岗人数低于最低要求的缺员区间。每名员工携带**可为空**的
日程列表（`schedules`，与 `/overlaps` 单侧参数与校验完全一致）；各日程按既有
跳时 / 叠时策略展开后，实例按真实时长解释为**半开 UTC 区间**
`[start, start+duration)` 并**裁剪到窗口**。同一员工相交或相接的区间先合并
（同一员工只计一人），再按时间扫描全部上下岗事件（**同一时刻的事件一起处理**）。
输出人数不足的非空区间；**在岗集合相同的相邻缺员段合并**，结果按起点排序；
全程达标时返回空列表。

请求体：

```json
{
  "window_start_utc": "2026-03-02T00:00:00Z",
  "window_end_utc": "2026-03-03T00:00:00Z",
  "min_staff": 2,
  "employees": [
    {
      "id": "alice",
      "schedules": [
        {
          "timezone": "Europe/Berlin",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "09:00",
          "duration_minutes": 60
        }
      ]
    },
    {
      "id": "bob",
      "schedules": [
        {
          "timezone": "America/New_York",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "03:30",
          "duration_minutes": 60
        }
      ]
    },
    {"id": "carol", "schedules": null}
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `window_start_utc` / `window_end_utc` | string | 查询窗口起止（半开），UTC ISO 8601 且**以 `Z` 结尾**；须递增且不超过 366 天 |
| `min_staff` | int | 最低在岗人数，`1`～员工总数 |
| `employees` | object[] | `1`～`20` 名员工，`id` 唯一 |
| `employees[].id` | string | 员工唯一 ID |
| `employees[].schedules` | object[] \| null | 日程列表（与 `/overlaps` 单侧相同）；`null` 或空列表表示无排班 |

响应 `200`（`gaps` 按 `start_utc` 排序）：

```json
{
  "count": 4,
  "gaps": [
    {"start_utc": "2026-03-02T00:00:00Z", "end_utc": "2026-03-02T08:00:00Z", "on_duty": [], "missing": 2},
    {"start_utc": "2026-03-02T08:00:00Z", "end_utc": "2026-03-02T08:30:00Z", "on_duty": ["alice"], "missing": 1},
    {"start_utc": "2026-03-02T09:00:00Z", "end_utc": "2026-03-02T09:30:00Z", "on_duty": ["bob"], "missing": 1},
    {"start_utc": "2026-03-02T09:30:00Z", "end_utc": "2026-03-03T00:00:00Z", "on_duty": [], "missing": 2}
  ]
}
```

上例中 alice 在岗 `[08:00, 09:00)` UTC、bob 在岗 `[08:30, 09:30)` UTC、carol 无排班；
仅 `[08:30, 09:00)` 达到 `min_staff=2`，其余时段按缺员输出。

| 字段 | 说明 |
| --- | --- |
| `count` | 缺员区间数 |
| `gaps[].start_utc` / `gaps[].end_utc` | 缺员区间起止（半开，UTC，Z 结尾） |
| `gaps[].on_duty` | 区间内在岗员工 ID，升序（可能为空列表） |
| `gaps[].missing` | 缺少人数（`min_staff` − 在岗人数） |

### `POST /rest-violations`

检查同一员工相邻工作段之间的休息是否低于最低分钟数。请求体携带该员工的
**一组日程**（`schedules`，与 `/overlaps` 单侧参数与校验完全一致，各含
`duration_minutes`）与 `min_rest_minutes`。各日程按既有跳时 / 叠时策略展开后，
实例按真实时长解释为**半开 UTC 区间** `[start, start+duration)`，并在真实 UTC
时间轴上**合并所有相交或相接的工作区间**（连续工作合并为一段，不产生空档，
故休息时长恒为正、不会出现负休息）。随后**仅比较相邻工作段之间的空档**，
返回严格低于阈值的空档：起止 UTC、实际休息分钟与差额，按起点排序。
跨时区与夏令时切换均按真实经过时间计算（如春跳当日钟面 3 小时仅为真实 2 小时）。

请求体：

```json
{
  "schedules": [
    {
      "timezone": "Europe/Berlin",
      "start_date": "2026-03-02",
      "end_date": "2026-03-02",
      "weekdays": [0],
      "local_time": "09:00",
      "duration_minutes": 240
    },
    {
      "timezone": "America/New_York",
      "start_date": "2026-03-02",
      "end_date": "2026-03-02",
      "weekdays": [0],
      "local_time": "08:00",
      "duration_minutes": 60
    }
  ],
  "min_rest_minutes": 90
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schedules` | object[] | 日程列表（与 `/overlaps` 单侧相同），至少 1 条 |
| `min_rest_minutes` | int | 相邻工作段之间的最低休息分钟数，`>= 1` |

响应 `200`（`violations` 按 `start_utc` 排序）：

```json
{
  "min_rest_minutes": 90,
  "count": 1,
  "violations": [
    {
      "start_utc": "2026-03-02T12:00:00Z",
      "end_utc": "2026-03-02T13:00:00Z",
      "rest_minutes": 60.0,
      "shortfall_minutes": 30.0
    }
  ]
}
```

上例中柏林 09:00 CET = 08:00 UTC 工作 240 分钟（至 12:00 UTC），
纽约 08:00 EST = 13:00 UTC 工作 60 分钟；两段之间真实空档
`[12:00, 13:00)` UTC 仅 60 分钟，低于阈值 90，差额 30 分钟。

| 字段 | 说明 |
| --- | --- |
| `min_rest_minutes` | 最低休息分钟数阈值 |
| `count` | 休息不足的空档数 |
| `violations[].start_utc` / `violations[].end_utc` | 空档起止（半开：起点为上一工作段终点，终点为下一工作段起点），UTC，Z 结尾 |
| `violations[].rest_minutes` | 实际休息分钟数（真实 UTC 流逝） |
| `violations[].shortfall_minutes` | 休息差额（`min_rest_minutes` − `rest_minutes`） |

空档恰好等于阈值不算违规；所有空档均达标（或工作段不足两段）时
`violations` 为空列表。

### `POST /common-free-slots`

在半开 UTC 窗口 `[window_start_utc, window_end_utc)` 内查询 **1～10 名参与者**
共同空闲、且能完整容纳一次预约的全部网格起点。每名参与者携带**可为空**的
日程列表（`schedules`，与 `/overlaps` 单侧参数与校验完全一致）；`null` 或
空列表表示该参与者**全程空闲**。各日程按既有跳时 / 叠时策略展开后，实例按
真实时长解释为**半开 UTC 区间** `[start, start+duration)`；同一参与者相交或
相接的忙碌段先合并（**重复实例不重复占用**），再取全体忙碌并集，窗口减去
并集即为共同空闲段。以**窗口起点为步长网格原点**（步长 `step_minutes`），
仅返回能完整容纳 `[t, t+duration_minutes)` 的网格起点 `t` 及对应终点
（**边界相接允许预约**），结果按 UTC 升序。

请求体：

```json
{
  "window_start_utc": "2026-03-02T08:00:00Z",
  "window_end_utc": "2026-03-02T12:00:00Z",
  "duration_minutes": 30,
  "step_minutes": 30,
  "participants": [
    {
      "schedules": [
        {
          "timezone": "Europe/Berlin",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "09:00",
          "duration_minutes": 60
        }
      ]
    },
    {
      "schedules": [
        {
          "timezone": "America/New_York",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "03:30",
          "duration_minutes": 60
        }
      ]
    },
    {"schedules": null}
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `window_start_utc` / `window_end_utc` | string | 查询窗口起止（半开），UTC ISO 8601 且**以 `Z` 结尾**；须递增且不超过 31 天 |
| `duration_minutes` | int | 预约时长（分钟），`1`～`1440` |
| `step_minutes` | int | 网格步长（分钟），`1`～`1440`；网格原点为窗口起点 |
| `participants` | object[] | `1`～`10` 名参与者 |
| `participants[].schedules` | object[] \| null | 日程列表（与 `/overlaps` 单侧相同）；`null` 或空列表表示全程空闲 |

响应 `200`（`slots` 按 `start_utc` 升序）：

```json
{
  "count": 5,
  "slots": [
    {"start_utc": "2026-03-02T09:30:00Z", "end_utc": "2026-03-02T10:00:00Z"},
    {"start_utc": "2026-03-02T10:00:00Z", "end_utc": "2026-03-02T10:30:00Z"},
    {"start_utc": "2026-03-02T10:30:00Z", "end_utc": "2026-03-02T11:00:00Z"},
    {"start_utc": "2026-03-02T11:00:00Z", "end_utc": "2026-03-02T11:30:00Z"},
    {"start_utc": "2026-03-02T11:30:00Z", "end_utc": "2026-03-02T12:00:00Z"}
  ]
}
```

上例中柏林 09:00 CET = 08:00 UTC 忙碌 60 分钟、纽约 03:30 EST = 08:30 UTC
忙碌 60 分钟，忙碌并集为 `[08:00, 09:30)`；第三名参与者全程空闲。共同空闲
`[09:30, 12:00)` 内，以 08:00 为原点、30 分钟步长的网格起点 `09:30` 起
均可完整容纳 30 分钟预约（`11:30` 的终点与窗口终点相接，允许）。

| 字段 | 说明 |
| --- | --- |
| `count` | 可用预约槽数 |
| `slots[].start_utc` / `slots[].end_utc` | 预约槽起止（半开，UTC，Z 结尾），终点 = 起点 + `duration_minutes` |

### `POST /schedule-changes`

对账同一员工**变更前后两版日程**在半开 UTC 窗口
`[window_start_utc, window_end_utc)` 内的工作时间差异。`before` / `after`
均为**可为空**的日程列表（与 `/overlaps` 单侧参数与校验完全一致）；
`null` 或空列表表示该版本无排班。两版日程各自按既有跳时 / 叠时策略展开，
实例按真实时长解释为**半开 UTC 区间** `[start, start+duration)`，
**裁剪到窗口**后分别**合并相交或相接的区间**（重复实例不重复占用）。
随后比较两版工作时间集合：**取消段 = 变更前 − 变更后，新增段 = 变更后 − 变更前**，
均输出为**最大连续区间**，附每段真实分钟数与两类总分钟数，分别按起点排序。
**仅实例条目变化而工作覆盖不变**（如一段 120 分钟拆成两段相接的 60 分钟）时，
`cancelled` 与 `added` 均为空列表。

请求体：

```json
{
  "window_start_utc": "2026-03-02T00:00:00Z",
  "window_end_utc": "2026-03-03T00:00:00Z",
  "before": [
    {
      "timezone": "Europe/Berlin",
      "start_date": "2026-03-02",
      "end_date": "2026-03-02",
      "weekdays": [0],
      "local_time": "09:00",
      "duration_minutes": 60
    }
  ],
  "after": [
    {
      "timezone": "America/New_York",
      "start_date": "2026-03-02",
      "end_date": "2026-03-02",
      "weekdays": [0],
      "local_time": "03:30",
      "duration_minutes": 60
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `window_start_utc` / `window_end_utc` | string | 查询窗口起止（半开），UTC ISO 8601 且**以 `Z` 结尾**；须递增且不超过 31 天 |
| `before` | object[] \| null | 变更前日程列表（与 `/overlaps` 单侧相同）；`null` 或空列表表示变更前无排班 |
| `after` | object[] \| null | 变更后日程列表（与 `/overlaps` 单侧相同）；`null` 或空列表表示变更后无排班 |

响应 `200`（`cancelled` / `added` 分别按 `start_utc` 排序）：

```json
{
  "cancelled_count": 1,
  "cancelled_total_minutes": 30.0,
  "cancelled": [
    {"start_utc": "2026-03-02T08:00:00Z", "end_utc": "2026-03-02T08:30:00Z", "minutes": 30.0}
  ],
  "added_count": 1,
  "added_total_minutes": 30.0,
  "added": [
    {"start_utc": "2026-03-02T09:00:00Z", "end_utc": "2026-03-02T09:30:00Z", "minutes": 30.0}
  ]
}
```

上例中变更前柏林 09:00 CET = `[08:00, 09:00)` UTC，变更后纽约 03:30 EST =
`[08:30, 09:30)` UTC；集合差得取消段 `[08:00, 08:30)` 与新增段
`[09:00, 09:30)`，各 30 分钟。

| 字段 | 说明 |
| --- | --- |
| `cancelled_count` / `added_count` | 取消 / 新增区间数 |
| `cancelled_total_minutes` / `added_total_minutes` | 取消 / 新增总真实分钟数 |
| `cancelled[]` / `added[]` | 取消 / 新增的最大连续区间，按起点排序；无差异时为空列表 |
| `start_utc` / `end_utc` | 差异区间起止（半开，UTC，Z 结尾） |
| `minutes` | 区间真实分钟数（UTC 流逝） |

### `POST /handoff-route`

在 **1～20 名员工**之间规划一条消息交接链：消息在 `message_at_utc` 产生时由
初始持有人 `holder_id` 获得，需经若干次**任意两人之间**的交接，最终不晚于
`deadline_utc` 到达目标员工 `target_id`。每次交接耗时固定
`duration_minutes`（`1`～`1440`），且必须在发送者与接收者的一段**连续共同
在岗**区间内**完整完成**；交接起点不得早于发送者实际持有消息的时刻（消息
产生或上一次交接结束），接收者在交接**结束时刻**获得消息。持有人**离岗期间
仍保留消息**，可等待之后的共同在岗时段。

每名员工携带**可为空**的日程列表（`schedules`，与 `/overlaps` 单侧参数与
校验完全一致）；`null` 或空列表表示该员工全程不在岗（除初始持有人外无法参与
交接）。各日程按既有跳时 / 叠时策略展开后，实例按真实时长解释为**半开 UTC
区间** `[start, start+duration)`；同一员工**相交或相接**的区间先合并为连续
在岗段（秋回两次实例相接时形成连续覆盖），两人的连续共同在岗段取各自合并后
区间的交集（**边界相接不算共同覆盖**）。服务在时间轴上做**时间相关最短路**
（迟到只会更晚到达，Dijkstra 适用），返回消息到达目标的**最早时刻**及一条
对应交接链；存在并列最早方案时任选其一。

请求体：

```json
{
  "message_at_utc": "2026-03-02T00:00:00Z",
  "deadline_utc": "2026-03-03T00:00:00Z",
  "duration_minutes": 30,
  "holder_id": "alice",
  "target_id": "carol",
  "employees": [
    {
      "id": "alice",
      "schedules": [
        {
          "timezone": "UTC",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "08:00",
          "duration_minutes": 120
        }
      ]
    },
    {
      "id": "bob",
      "schedules": [
        {
          "timezone": "UTC",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "08:30",
          "duration_minutes": 60
        },
        {
          "timezone": "UTC",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "11:00",
          "duration_minutes": 60
        }
      ]
    },
    {
      "id": "carol",
      "schedules": [
        {
          "timezone": "UTC",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "11:00",
          "duration_minutes": 120
        }
      ]
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `message_at_utc` | string | 消息产生时刻（初始持有人此时获得消息），UTC ISO 8601 且**以 `Z` 结尾** |
| `deadline_utc` | string | 到达截止时刻（交接终点须 **≤** 该时刻），UTC ISO 8601 且**以 `Z` 结尾**；两时刻须严格递增且相距不超过 31 天 |
| `duration_minutes` | int | 每次交接的时长（分钟），`1`～`1440` |
| `holder_id` | string | 初始持有人唯一 ID，必须引用 `employees` 中存在的员工 |
| `target_id` | string | 目标员工唯一 ID，必须引用 `employees` 中存在的员工 |
| `employees` | object[] | `1`～`20` 名员工，`id` 唯一 |
| `employees[].id` | string | 员工唯一 ID |
| `employees[].schedules` | object[] \| null | 日程列表（与 `/overlaps` 单侧相同）；`null` 或空列表表示无排班 |

响应 `200`（`chain` 按交接先后排列）：

```json
{
  "arrival_utc": "2026-03-02T11:30:00Z",
  "count": 2,
  "chain": [
    {
      "sender_id": "alice",
      "receiver_id": "bob",
      "start_utc": "2026-03-02T08:30:00Z",
      "end_utc": "2026-03-02T09:00:00Z"
    },
    {
      "sender_id": "bob",
      "receiver_id": "carol",
      "start_utc": "2026-03-02T11:00:00Z",
      "end_utc": "2026-03-02T11:30:00Z"
    }
  ]
}
```

上例中 alice 在岗 `[08:00, 10:00)` UTC、bob 在岗 `[08:30, 09:30)` 与
`[11:00, 12:00)`、carol 在岗 `[11:00, 13:00)`；alice 与 bob 的连续共同
在岗 `[08:30, 09:30)` 内完成 30 分钟交接，bob 于 `09:00` 持有消息；bob
离岗期间消息保留，`11:00` 再与 carol 共同在岗时交接，carol 于 `11:30`
获得（alice 与 carol 全程无交集，故必须中转）。

| 字段 | 说明 |
| --- | --- |
| `arrival_utc` | 消息到达目标的最早时刻（UTC，Z 结尾）；无解时为 `null` |
| `count` | 交接次数（`chain` 长度） |
| `chain[].sender_id` / `receiver_id` | 该次交接的发送者（消息当前持有人）与接收者 ID |
| `chain[].start_utc` / `end_utc` | 交接半开区间起止（UTC，Z 结尾）；`end_utc − start_utc = duration_minutes`，接收者在 `end_utc` 获得消息 |

特殊情形：

- **初始持有人即目标**（`holder_id == target_id`）：无需交接，
  `arrival_utc` 直接为 `message_at_utc`，`count` 为 0、`chain` 为空。
- **无解**（窗口内不存在能在截止前完整交接至目标的共同在岗链）：
  `arrival_utc` 为 `null`，`count` 为 0、`chain` 为空。交接终点恰好等于
  `deadline_utc` 仍算按时到达。
- 同一名员工**多段相接或相交**的日程（含秋回 `fold_strategy=both` 产生的
  两段）先合并为一段连续在岗；跨班次连续在岗期间可完成单次交接。
- 发送者在共同在岗段开始时若尚未持有消息，交接从其实际持有的时刻起算
  （如消息产生于一段共同在岗的中间）。

### `POST /inventory-allocation`

仓储人员把订单数量分配到库存批次：按 **FEFO（先到期先出）** 依次扣减——批次按
`expiry_date` 从早到晚排序，**同日按 `batch_no` Unicode 字典序**排序；订单按
输入顺序逐条满足，**允许一条订单拆分到多个批次**。只有当**全部订单都能足量
满足**时才整批提交返回；任一订单库存不足则整批拒绝（`409`），**不返回任何
部分分配**。数量用十进制精确运算，支持整数与小数（如 `5`、`"2.5"`）。

请求体：

```json
{
  "batches": [
    {"batch_no": "B01", "available_quantity": 7,    "expiry_date": "2026-01-01"},
    {"batch_no": "B02", "available_quantity": 5.5,  "expiry_date": "2026-03-01"},
    {"batch_no": "B10", "available_quantity": 3,    "expiry_date": "2026-03-01"},
    {"batch_no": "B9",  "available_quantity": 100,  "expiry_date": "2026-06-01"}
  ],
  "orders": [
    {"order_id": "SO-1001", "quantity": 10},
    {"order_id": "SO-1002", "quantity": 5.5}
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `batches` | object[] | 库存批次，至少 1 个；`batch_no` 在请求内唯一、非空 |
| `batches[].batch_no` | string | 批次号（去除首尾空白后非空且唯一） |
| `batches[].available_quantity` | number \| string | 可用数量，**必须为正数**，整数或十进制小数 |
| `batches[].expiry_date` | string | 失效日期，严格 `YYYY-MM-DD`（ISO 8601） |
| `orders` | object[] | 订单明细，至少 1 条；`order_id` 在请求内唯一、非空 |
| `orders[].order_id` | string | 订单号（去除首尾空白后非空且唯一） |
| `orders[].quantity` | number \| string | 订单需求数量，**必须为正数**，整数或十进制小数 |

响应 `200`：

```json
{
  "allocations": [
    {
      "order_id": "SO-1001",
      "quantity": 10,
      "lines": [
        {"batch_no": "B01", "expiry_date": "2026-01-01", "quantity": 7},
        {"batch_no": "B02", "expiry_date": "2026-03-01", "quantity": 3}
      ]
    },
    {
      "order_id": "SO-1002",
      "quantity": 5.5,
      "lines": [
        {"batch_no": "B02", "expiry_date": "2026-03-01", "quantity": 2.5},
        {"batch_no": "B10", "expiry_date": "2026-03-01", "quantity": 3}
      ]
    }
  ],
  "remaining_stock": [
    {"batch_no": "B01", "expiry_date": "2026-01-01", "available_before": 7,   "remaining": 0},
    {"batch_no": "B02", "expiry_date": "2026-03-01", "available_before": 5.5, "remaining": 0},
    {"batch_no": "B10", "expiry_date": "2026-03-01", "available_before": 3,   "remaining": 0},
    {"batch_no": "B9",  "expiry_date": "2026-06-01", "available_before": 100, "remaining": 100}
  ],
  "total_allocated": 15.5
}
```

| 字段 | 说明 |
| --- | --- |
| `allocations[]` | 每条订单的分配结果，按订单输入顺序返回 |
| `allocations[].quantity` | 订单原始需求数量；`lines` 中各 `quantity` 之和与其相等 |
| `allocations[].lines[]` | 按 FEFO 取用顺序排列的批次明细（一条订单可跨多个批次，数量为 0 的批次不会出现） |
| `remaining_stock[]` | 各批次分配前/后数量，按失效日期、批次号（FEFO 顺序）排序 |
| `remaining_stock[].available_before` / `remaining` | 分配前可用量 / 分配后剩余量 |
| `total_allocated` | 总分配量（全部订单需求量之和） |

库存不足时返回 `409 Conflict`，响应体指出第一条无法足量满足的订单及缺口，
且不含任何分配结果（整批原子拒绝）：

```json
{
  "detail": {
    "error": "insufficient_stock",
    "order_id": "SO-1001",
    "requested": 10,
    "shortfall": 3,
    "total_available": 7
  }
}
```

`shortfall` 为该订单在当时剩余库存下仍缺少的数量，`total_available` 为本次请求
全部批次的可用量合计。输入校验失败（数量非正、批次号 / 订单号重复或为空、
日期格式错误、列表为空等）统一返回 `422`，详见下节。

### `POST /receipt-write-off`

商户试算一批收款对发票的核销。输入发票（发票号、到期日、应收金额）以及**按顺序
排列**的收款 / 撤回事件。处理规则：

- **核销顺序**：发票按 `due_date` 从早到晚排序，**同日按 `invoice_no` Unicode
  字典序**排序；每笔收款依次填补排好序发票的当前未结金额，**单笔收款可拆分核销
  到多张发票**。
- **撤回只释放原额度**：撤回按 `reversal_of` 找到目标收款，**只把该收款原先核销
  的那些金额逐笔加回**；已处理的其他收款保持原样、**不重新分配**。释放出的额度
  供**后续**收款使用。
- **幂等重放**：`event_id` 相同代表同一事件重放——**内容完全一致只计一次**
  （收款不再次核销，撤回不再次释放，直接跳过）；**内容不同则整批拒绝**
  （金额不一致、撤回目标不一致、收款 / 撤回类型互换都算内容不同）。
- **非法撤回整批拒绝**：撤回目标在该撤回之前不存在（事件号不存在、指向撤回事件、
  指向尚未出现的后续事件、自引用），或该收款**已经被撤回**。
- **超额收款整批拒绝**：收款金额**严格大于**处理当时的全部发票剩余应收合计
  （恰好相等允许，发票恰好全部结清）。
- 金额必须为**正数且至多两位小数**，全程以 `Decimal` **按分精确计算**；任何
  上述业务冲突都返回 `409`，**不返回任何部分试算**。

请求体：

```json
{
  "invoices": [
    {"invoice_no": "INV-003", "due_date": "2026-02-01", "receivable": "10.00"},
    {"invoice_no": "INV-001", "due_date": "2026-01-01", "receivable": 5},
    {"invoice_no": "INV-002", "due_date": "2026-01-01", "receivable": 7}
  ],
  "events": [
    {"type": "receipt",  "event_id": "R1", "amount": "20.00"},
    {"type": "receipt",  "event_id": "R1", "amount": "20.00"},
    {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
    {"type": "receipt",  "event_id": "R2", "amount": "22.00"}
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `invoices` | object[] | 发票，至少 1 张；`invoice_no` 在请求内唯一、非空（去除首尾空白后） |
| `invoices[].invoice_no` | string | 发票号（去除首尾空白后非空且唯一） |
| `invoices[].due_date` | string | 到期日，严格 `YYYY-MM-DD`（ISO 8601） |
| `invoices[].receivable` | number \| string | 应收金额，**正数且至多两位小数** |
| `events` | object[] | 按顺序处理的事件，至少 1 个；以 `type` 区分收款 / 撤回 |
| `events[].type` | string | `receipt`（收款）或 `reversal`（撤回） |
| 收款 `events[].event_id` | string | 事件号，非空；**相同事件号视为同一事件的重放** |
| 收款 `events[].amount` | number \| string | 收款金额，**正数且至多两位小数** |
| 撤回 `events[].reversal_of` | string | 被撤回的**先前收款**事件号（去除首尾空白后非空） |

响应 `200`（`receipts` 按事件处理顺序；`invoices` 按到期日、发票号排序）：

```json
{
  "receipts": [
    {
      "event_id": "R2",
      "amount": 22,
      "lines": [
        {"invoice_no": "INV-001", "due_date": "2026-01-01", "amount": 5},
        {"invoice_no": "INV-002", "due_date": "2026-01-01", "amount": 7},
        {"invoice_no": "INV-003", "due_date": "2026-02-01", "amount": 10}
      ]
    }
  ],
  "invoices": [
    {"invoice_no": "INV-001", "due_date": "2026-01-01", "receivable": 5,  "outstanding": 0},
    {"invoice_no": "INV-002", "due_date": "2026-01-01", "receivable": 7,  "outstanding": 0},
    {"invoice_no": "INV-003", "due_date": "2026-02-01", "receivable": 10, "outstanding": 0}
  ],
  "total_written_off": 22
}
```

上例中 `R1` 收款 20 后被 `V1` 撤回（其核销额全部释放，`R1` 不再出现在
`receipts` 中），紧随其后的同内容 `R1` 重放只计一次（跳过）；`R2` 收款 22
恰好结清三张发票（到期顺序 INV-001 / INV-002 同日，再 INV-003）。

| 字段 | 说明 |
| --- | --- |
| `receipts[]` | 各**有效收款**（未被撤回）的核销明细，按事件处理顺序；同内容重放不重复输出 |
| `receipts[].event_id` / `amount` | 收款事件号 / 收款原始金额；`lines` 各金额之和与其相等 |
| `receipts[].lines[]` | 按到期日、发票号填补顺序排列的核销明细（可跨多张发票，金额为 0 不出现） |
| `invoices[]` | 每张发票的未结情况，按到期日、发票号排序；含全部输入发票（即使未被核销） |
| `invoices[].receivable` / `outstanding` | 原始应收金额 / 试算后未结金额 |
| `total_written_off` | 有效收款的核销总额（等于各发票应收 − 未结之和） |

业务冲突时返回 `409 Conflict`，响应体只含 `detail`，**不含任何核销结果**
（整批原子拒绝）。三类错误：

```json
// 收款超过当时剩余应收合计
{"detail": {"error": "receipt_exceeds_receivable", "event_id": "R1",
            "amount": 22.01, "remaining_receivable": 22, "shortfall": 0.01}}

// 撤回目标不存在 / 已撤回（reason: not_found / already_reversed）
{"detail": {"error": "reversal_target_invalid", "event_id": "V2",
            "referenced_event_id": "R1", "reason": "already_reversed"}}

// 相同事件号内容不一致的重放
{"detail": {"error": "inconsistent_replay", "event_id": "R1"}}
```

输入形状 / 取值校验失败（金额非正、超过两位小数、不是数字、`due_date` 不是
严格 `YYYY-MM-DD`、发票号为空白或重复、`type` 不是 `receipt` / `reversal`、
撤回缺少 `reversal_of`、`invoices` / `events` 为空或缺失、`NaN` / `Infinity`
等）统一返回 `422`，详见下节。

### 错误（422）

以下情况返回 `422 Unprocessable Entity`：

- 非法 IANA 时区名（含 `/daily-occupancy` 的 `report_timezone`）
- `end_date` 早于 `start_date`（逆序区间）
- 区间超过 366 天（`(end_date - start_date).days > 366`，`/overlaps` 对每侧分别限制）
- `duration_minutes` 不在 1～1440（仅 `/overlaps` 与 `/daily-occupancy`）
- 星期值越界（不在 0–6）或集合为空等其他字段校验失败
- `/staffing-gaps`：窗口起止不是以 `Z` 结尾的 UTC 时刻、窗口未递增或超过 366 天、
  `min_staff` 不在 1～员工总数、员工数不在 1～20、员工 ID 重复
- `/rest-violations`：`schedules` 为空、`min_rest_minutes` 小于 1，
  或任一日程字段校验失败（规则与 `/overlaps` 单侧一致）
- `/common-free-slots`：窗口起止不是以 `Z` 结尾的 UTC 时刻、窗口未递增或
  超过 31 天、`duration_minutes` / `step_minutes` 不在 1～1440、
  参与者数不在 1～10，或任一参与者的日程字段校验失败
  （规则与 `/overlaps` 单侧一致）
- `/schedule-changes`：窗口起止不是以 `Z` 结尾的 UTC 时刻、窗口未递增或
  超过 31 天，或任一版本的日程字段校验失败（规则与 `/overlaps` 单侧一致）
- `/handoff-route`：`message_at_utc` / `deadline_utc` 不是以 `Z` 结尾的
  UTC 时刻、两时刻未严格递增或相距超过 31 天、`duration_minutes` 不在
  1～1440、员工数不在 1～20、员工 ID 重复、`holder_id` / `target_id`
  引用不存在的员工，或任一员工的日程字段校验失败
  （规则与 `/overlaps` 单侧一致）。窗口内无法在截止前交接至目标不是
  非法输入，返回 `200` 且 `arrival_utc` 为 `null`
- `/inventory-allocation`：`batches` / `orders` 为空或缺失、批次量或订单量
  非正（≤ 0、`NaN` / `Infinity`）或不是数字、批次号 / 订单号为空白或重复、
  `expiry_date` 不是严格 `YYYY-MM-DD`（如 `2026/01/01`、`2026-02-30`）。
  库存不足不是 422 而是 `409`，整批拒绝且不返回部分分配
- `/receipt-write-off`：`invoices` / `events` 为空或缺失、应收 / 收款金额
  非正（≤ 0、`NaN` / `Infinity`）或不是数字、金额**超过两位小数**
  （如 `0.001`、`10.123`）、发票号 / 事件号 / `reversal_of` 为空白、
  发票号重复、`due_date` 不是严格 `YYYY-MM-DD`、事件缺少 `type` 或 `type`
  不是 `receipt` / `reversal`（判别字段）、撤回缺少 `reversal_of`。
  收款超额、撤回目标不存在或已撤回、相同事件号重放内容不同均不是 422 而是
  `409`，整批拒绝且不返回部分试算；相同事件号且内容一致的重放是合法幂等
  重放（事件号允许重复），返回 `200` 且只计一次

## curl 请求示例

```bash
# /handoff-route：alice 与 carol 无共同在岗，消息经 bob 中转交接
curl -s http://localhost:8000/handoff-route \
  -H 'Content-Type: application/json' \
  -d '{
    "message_at_utc": "2026-03-02T00:00:00Z",
    "deadline_utc": "2026-03-03T00:00:00Z",
    "duration_minutes": 30,
    "holder_id": "alice",
    "target_id": "carol",
    "employees": [
      {"id": "alice", "schedules": [{
        "timezone": "UTC", "start_date": "2026-03-02", "end_date": "2026-03-02",
        "weekdays": [0], "local_time": "08:00", "duration_minutes": 120}]},
      {"id": "bob", "schedules": [
        {"timezone": "UTC", "start_date": "2026-03-02", "end_date": "2026-03-02",
         "weekdays": [0], "local_time": "08:30", "duration_minutes": 60},
        {"timezone": "UTC", "start_date": "2026-03-02", "end_date": "2026-03-02",
         "weekdays": [0], "local_time": "11:00", "duration_minutes": 60}]},
      {"id": "carol", "schedules": [{
        "timezone": "UTC", "start_date": "2026-03-02", "end_date": "2026-03-02",
        "weekdays": [0], "local_time": "11:00", "duration_minutes": 120}]}
    ]'

# /inventory-allocation：FEFO 分配，SO-1001 拆到 B01+B02，SO-1002 再用 B02+B10
curl -s http://localhost:8000/inventory-allocation \
  -H 'Content-Type: application/json' \
  -d '{
    "batches": [
      {"batch_no": "B01", "available_quantity": 7,   "expiry_date": "2026-01-01"},
      {"batch_no": "B02", "available_quantity": 5.5, "expiry_date": "2026-03-01"},
      {"batch_no": "B10", "available_quantity": 3,   "expiry_date": "2026-03-01"},
      {"batch_no": "B9",  "available_quantity": 100, "expiry_date": "2026-06-01"}
    ],
    "orders": [
      {"order_id": "SO-1001", "quantity": 10},
      {"order_id": "SO-1002", "quantity": 5.5}
    ]}'

# /receipt-write-off：R1 收款 20 被撤回并释放，R2 收款 22 结清三张发票
curl -s http://localhost:8000/receipt-write-off \
  -H 'Content-Type: application/json' \
  -d '{
    "invoices": [
      {"invoice_no": "INV-003", "due_date": "2026-02-01", "receivable": "10.00"},
      {"invoice_no": "INV-001", "due_date": "2026-01-01", "receivable": 5},
      {"invoice_no": "INV-002", "due_date": "2026-01-01", "receivable": 7}
    ],
    "events": [
      {"type": "receipt",  "event_id": "R1", "amount": "20.00"},
      {"type": "reversal", "event_id": "V1", "reversal_of": "R1"},
      {"type": "receipt",  "event_id": "R2", "amount": "22.00"}
    ]}'
```

## 判定原理

对每个候选本地时刻分别按 `fold=0` / `fold=1` 赋予时区，换算到 UTC 再换算回来
（UTC 往返校验）：能还原原时刻的解释才真实存在。两种解释均有效且 UTC 相同为正常；
均有效但 UTC 不同为叠时；均无效为跳时，跳变差值取两种解释对应的 UTC 之差，
`shift_forward` 即将本地时刻加上该差值后重新校验。所有实例按 UTC 去重
（保留先出现者）并按 UTC 稳定排序。

库存分配（`/inventory-allocation`）把批次按 `(失效日期, 批次号)` 稳定排序后，
按订单输入顺序逐条贪心扣减：每条订单依次从当前尚有库存的最早批次取用
`min(需求量, 批次剩余)`，直到该单需求归零；任一订单遍历完批次后仍有缺口，
则整体判定失败、丢弃全部模拟扣减（不写入、不返回部分结果）。数量全程以
`Decimal` 运算，避免浮点误差。

收款核销（`/receipt-write-off`）把发票按 `(到期日, 发票号)` 稳定排序后，
按事件输入顺序维护每张发票的当前未结金额：收款依次取
`min(剩余收款额, 发票未结)` 填补，且在核销前先断言收款额不超过当时全部未结合计，
超出即整批失败；撤回不参与重排，只按目标收款记录的原核销明细逐笔加回
（目标必须是当前生效、未撤回的收款，否则整批失败）。每个事件号首次出现时记录
内容签名（收款记金额，撤回记 `reversal_of`，类型本身也参与比较），再次出现时
签名相同则跳过、不同则整批失败。金额全程以 `Decimal` 运算并以 **0.01（分）**
为最小刻度，整数金额序列化为 JSON 整数、否则序列化为两位以内小数。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # 运行测试
```

## 项目结构

```
app/
  main.py        # FastAPI 入口，/expand、/overlaps、/daily-occupancy、/staffing-gaps、/rest-violations、/common-free-slots、/schedule-changes、/handoff-route、/inventory-allocation、/receipt-write-off 与 /health
  models.py      # 请求/响应模型与 422 校验规则
  scheduler.py   # UTC 往返校验分类 + 展开 / 去重 / 排序 + 扫描线重叠检测 + 逐日占用统计 + 在岗缺口扫描 + 休息违规检查 + 共同空闲预约槽枚举 + 排班变更集合差对账 + 共同在岗区间交集与交接链最短路
  inventory.py   # FEFO 批次分配：按 (失效日期, 批次号) 贪心扣减 + 库存不足整批拒绝
  receivables.py # 收款核销试算：按 (到期日, 发票号) 顺序填补 + 撤回仅释放原核销额 + 幂等重放 + 超额/非法撤回/重放冲突整批拒绝（Decimal 按分精确运算）
tests/           # pytest 测试（核心逻辑 + API）
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
