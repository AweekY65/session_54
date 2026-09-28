# localtrace — 本地分布式 Trace 收集与分析工具

一个**纯本地**的分布式链路追踪收集器与分析器：所有 span、trace、索引、统计和报告
只存储在本地文件或内存中。

- ❌ 不依赖 Jaeger / Zipkin / OpenTelemetry Collector / 任何数据库 / 云监控服务
- ❌ 无第三方 Python 依赖（仅标准库，Python ≥ 3.10）
- ✅ 接收乱序到达的 span，父子关系永不丢失
- ✅ 重建 trace tree，检测孤儿 span、重复 span ID、循环父子关系
- ✅ 计算 trace 总延迟、各 service 耗时（exclusive/self time，不重复累计嵌套）和 critical path
- ✅ 按 service / status / attribute / 时间范围过滤，输出**确定性** JSON 与文本报告
- ✅ 本地 retention 清理与索引重建，清理后查询绝不返回已删除数据
- ✅ 内置本地 HTTP 接收端和以本地函数 / 独立进程模拟的测试服务

---

## 目录

- [快速开始](#快速开始)
- [Trace 模型](#trace-模型)
- [树重建与异常检测](#树重建与异常检测)
- [耗时计算：为什么嵌套时间不会重复累计](#耗时计算为什么嵌套时间不会重复累计)
- [Critical Path 算法](#critical-path-算法)
- [过滤与报告](#过滤与报告)
- [本地文件格式](#本地文件格式)
- [Retention 与索引重建](#retention-与索引重建)
- [HTTP 接收端与模拟服务](#http-接收端与模拟服务)
- [Python API](#python-api)
- [测试](#测试)
- [项目结构](#项目结构)

---

## 快速开始

```bash
# 1) 导入示例 span（JSON 数组，也支持 JSONL）
python -m localtrace --data-dir ./tracedata import examples/spans.json

# 2) 文本报告（含完整 span 树，* 标记 critical path）
python -m localtrace --data-dir ./tracedata report --format text --detailed

# 3) 过滤查询
python -m localtrace --data-dir ./tracedata query --service payment
python -m localtrace --data-dir ./tracedata query --status ERROR
python -m localtrace --data-dir ./tracedata query --attr env=dev --attr order_id=42
python -m localtrace --data-dir ./tracedata query --start 1000000 --end 1000300

# 4) 生成一个内置模拟 trace（7 个 span、7 个 service、含并行分支），直接入库
python -m localtrace --data-dir ./tracedata simulate --receive --seed 1

# 5) retention：删除结束时间 <= 500000us 的整条 trace
python -m localtrace --data-dir ./tracedata retain --before 500000

# 6) 索引重建
python -m localtrace --data-dir ./tracedata reindex
```

时间参数既可传**整数微秒**（`1000000`），也可传 ISO-8601
（`2026-09-28T10:00:00.000001Z`）。

### 启动本地接收服务

```bash
python -m localtrace --data-dir ./tracedata serve --port 8765
# 另一个终端：
curl -s -X POST http://127.0.0.1:8765/spans \
  -H 'Content-Type: application/json' \
  -d '[{"trace_id":"t1","span_id":"a","service":"gateway",
        "start_us":0,"end_us":10}]'
# 或用 stdin
cat examples/spans.json | python -m localtrace --data-dir ./tracedata ingest
```

---

## Trace 模型

一个 **Span** 包含：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 所属 trace（必填） |
| `span_id` | span 标识；在同一 trace 内唯一 |
| `parent_id` | 父 span 标识；空串 / `0` / 全 0 表示根 span |
| `service` | 产生该 span 的服务名 |
| `name` | 操作名（如 `GET /checkout`） |
| `start_us` / `end_us` | 起止时间，**整数微秒**；也接受 ISO-8601 字符串 |
| `duration_us` | 派生值 `end - start`，不允许为负 |
| `status` | `OK` / `ERROR` / `UNSET`（兼容 `success`/`failure` 等别名） |
| `attributes` | 自由键值对（数字、字符串、布尔、数组、嵌套对象均可） |

身份键是 `(trace_id, span_id)`。trace 由若干 span 通过 `parent_id` 组成一棵
（或异常情况下的多棵）树；span 到达顺序无关。

导入 JSON 时还支持字段别名 `traceId` / `spanId` / `parentId` / `startTime` 等，
并可用 `start` + `duration_us`（或 `duration_ms`）代替 `end`。

---

## 树重建与异常检测

`localtrace.tree.build_trace_tree(spans)` 接收**任意顺序**的 span（子先于父到达也
没问题——全部 span 先入索引，再统一连边），输出：

- `roots`：根 span 列表（按开始时间、id 排序，顺序稳定）
- `children`：`parent_id -> 子 span id 列表`（已排序）
- `order`：确定性前序 DFS 遍历（父先于子）
- `anomalies`：结构异常列表

检测三类异常：

1. **orphan_span（孤儿）**：`parent_id` 指向的 span 不存在。该 span 仍作为一个
   额外根挂回森林，保证不丢失、仍可查询，异常中给出缺失的父 id。
2. **duplicate_span_id（重复 ID）**：同一 trace 内两个 span 的 `span_id` 相同。
   **先到先得**，后到的同 id span 被拒绝入库（若内容完全一致则视为幂等重投，
   记为 `duplicate_identical`；内容冲突记为 `duplicate_conflict`）。
3. **cycle（循环父子关系）**：沿 parent 指针成环（含自指）。检测使用带记忆化的
   parent 链遍历（O(n)），对每个环输出一次，环节点按字典序最小 id 规范化旋转
   （同一环无论从哪进入，报告完全一致）。随后**切断环上一条边**（最小环节点的
   parent 指针），使图恢复成森林，后续分析照常进行。

---

## 耗时计算：为什么嵌套时间不会重复累计

朴素地把所有 span 的 `end - start` 相加，会把嵌套子 span 的时间在父 span 里
重复计算。localtrace 对每个 span 计算两种时间：

- **inclusive time（墙钟时间）**：span 自身的 `end - start`，其中包含所有子 span。
  报告中同时给出每个 service 的 inclusive 合计，用于观察“某服务参与了多久”。
- **exclusive time / self time（独占时间）**：

```
exclusive(parent) = duration(parent)
                  − length( union( 直接子 span 的时间区间 ) )
```

关键点：**先把互相重叠的并行子 span 区间做并集合并（merge intervals），再相减**。
因此两个并行子 span 重叠的部分只会被扣一次，父 span 自身时间不会被扣成负数。

> 一个父 span 的 exclusive + 其直接子 span 区间并集长度 = 该父 span 的墙钟时长。
> 各 service 的 exclusive 合计是“真正花在该服务自身、不含任何下游”的时间，
> 嵌套不会重复累计。

trace 总延迟（`duration_us`）= 全部 span 中 `min(start)` 到 `max(end)`。
trace 状态 = 任一 span 为 `ERROR` 则 `ERROR`，否则全 `UNSET` 为 `UNSET`，其余 `OK`。

---

## Critical Path 算法

Critical path 是决定整个 trace 时长的那条 span 链——优化链上的耗时才能缩短
端到端延迟。算法：

1. 从**结束最晚的根**开始（平局按最早开始、span id 决定，保证确定性）。
2. 在当前 span 的直接子节点中，选择**结束时间最晚**的子节点——即父 span 结束时
   它仍在运行、真正“卡住”父节点的那条分支。
3. 重复直到叶子，得到链 `C = [c₀, c₁, …, cₖ]`。
4. 每个链节点的**贡献**为它的 exclusive 时间（裁剪到根 span 的时间窗内）。由于
   链上的 span 彼此嵌套，而每个 span 的 exclusive 已排除其所有直接子节点（包括
   链上的下一个节点），各层贡献天然互斥、绝不重叠：

```
total(critical path) = duration(根 span)
Σ contribution(cᵢ)    ≤ total
parallel_time_us      = total − Σ contribution(cᵢ)
```

`parallel_time_us` 即非关键分支上的并行工作与链段间空隙的总时间。

示例（`examples/spans.json`）：

```
critical path (600us, parallel=180us): s1 -> s3 -> s5 -> s7
```

`s6(notify)` 与 `s4(inventory)` 是 `s3` 下与支付链并行的分支，其耗时体现在
`parallel_time_us`，不会被错误地加到关键路径上。

---

## 过滤与报告

过滤器之间为 **AND**：

- `--service a --service b`：trace 中**同时**出现 a 和 b（多次指定为 AND）。
- `--status ERROR --status UNSET`：trace 汇总状态命中其一（OR）。
- `--attr key`：某 span 上存在该键；
  `--attr key=value`：某 span 上该键等于 value（同时做字符串/结构化比较，
  值在数组里也算命中）；
  `--attr 'x:=true'` 或 `--attr 'x:=json:3'`：按 JSON 类型解析值。
- `--start <t> --end <t>`：trace 的 `[start,end]` 与查询窗口**有重叠**即命中。
- `--trace-id`：限定 trace（可重复）；`--limit`：截断结果数。

查询结果按 `(trace 开始时间, 时长, trace_id)` 稳定排序。

报告由 `report` 子命令输出：

- `--format json`（默认）：固定键顺序、集合全部排序、不读时钟——**同一份数据
  永远产生逐字节相同的 JSON**，适合 diff 与快照测试。
- `--format text`：等宽文本报告；`--detailed` 附带完整 span 树，`*` 标记
  critical path 上的 span，并显示每个 span 的 `self` 独占时间。
- `-o file`：写入本地文件。

JSON 报告包含 store 概况、命中统计、状态计数、时长 min/max/avg/total、
各 service exclusive 合计、异常 trace 数，以及每条 trace 的摘要或完整树。

---

## 本地文件格式

所有数据位于 `--data-dir`（默认 `.localtrace/`）：

```
tracedata/
├── spans.jsonl   # 唯一事实来源：每行一个 span 的规范化 JSON（append-only）
├── index.json    # 派生索引/校验信息（可随时删除并重建）
└── meta.json     # 格式版本与计数
```

### `spans.jsonl`

一行一个 span，键序固定，例如：

```json
{"attributes":{"env":"dev"},"duration_us":10,"end_us":10,"name":"op","parent_id":"","service":"gateway","span_id":"a","start_us":0,"status":"OK","trace_id":"t1"}
```

写入采用**追加 + fsync**；retention 时通过“临时文件 + 原子 rename”整体重写。

### `index.json`

```json
{
  "checksum": "spans.jsonl 的 SHA-256",
  "format_version": 1,
  "span_count": 7,
  "trace_count": 1
}
```

索引只是缓存，**不是事实来源**：打开 store 时若发现 `index.json` 缺失、损坏或
checksum 与 `spans.jsonl` 不一致，会自动从 spans 文件重建；`reindex` 命令可随时
强制重建。

---

## Retention 与索引重建

retention 以 **trace 为粒度**整体删除，不会留下半条 trace：

- `retain --before <t>`：删除**结束时间 ≤ t** 的 trace（标准的“丢弃早于 T 的数据”）。
- `retain --after <t>`：删除**开始时间 ≥ t** 的 trace。
- `retain --max-traces N`：只保留最近开始的 N 条 trace（按开始时间降序、id 破平）。

删除会原子重写 `spans.jsonl` 并立即重建内存索引，因此**清理之后的任何查询都不可能
返回已删除数据**（测试 `test_queries_after_retention_return_nothing_deleted` 与
`test_retention_survives_reload` 专门验证这一点，包括重开进程后仍然成立）。

`reindex` 丢弃全部内存索引，从 `spans.jsonl` 重新扫描构建并刷新 `index.json`。

---

## HTTP 接收端与模拟服务

### 接收端（仅标准库 `http.server`，默认绑定 127.0.0.1）

- `POST /spans`：body 为单个 span 对象、span 数组或 `{"spans": [...]}`。
  成功返回 `202` 与入库计数；非法 JSON / 非法 span 返回 `400`，不会半批写入。
- `GET /healthz`：`{"status":"ok","spans":N}`。

### 模拟服务（本地函数 / 进程，无任何外部依赖）

- `localtrace.simulator.simulate_trace(...)`：在**内存中**生成一个多服务 trace
  （gateway → auth/checkout → inventory/payment/notify → payment-db，含并行分支），
  并**故意打乱 span 顺序**返回，用于压测乱序入库路径；某些 seed 下会注入一个
  `ERROR` span。
- `python -m localtrace.simulator --url http://127.0.0.1:8765/spans`：以
  **独立操作系统进程**运行（`subprocess` 启动），通过真实 HTTP POST 把 span 发给
  本地接收端——在真实进程边界下验证全链路，同时不依赖任何外部服务。
- `--print` 仅把生成的 span 以 JSON 打到标准输出。

---

## Python API

```python
from localtrace import Collector, SpanStore
from localtrace.query import TraceFilter

store = SpanStore("./tracedata")
collector = Collector(store)

# 乱序接收（子 span 先到）
collector.receive([
    {"trace_id": "t1", "span_id": "gc", "parent_id": "c1",
     "service": "db", "start_us": 20, "end_us": 60},
    {"trace_id": "t1", "span_id": "c1", "parent_id": "r",
     "service": "api", "start_us": 10, "end_us": 80},
    {"trace_id": "t1", "span_id": "r", "service": "gateway",
     "start_us": 0, "end_us": 100},
])

analysis = store.analyze_trace("t1")
print(analysis.duration_us)                 # 100
print(analysis.service_time_us)             # {'api': 30, 'db': 40, 'gateway': 30}
print(analysis.critical_path.span_ids)      # ['r', 'c1', 'gc']
print([a.kind for a in analysis.anomalies]) # []

results = collector.query(TraceFilter(services=["db"]))
store.retain(before_us=50)                  # 本地 retention
store.reindex()                             # 索引重建
```

---

## 测试

全部为终端自动化测试（pytest，标准库 + pytest）：

```bash
# 在项目根目录执行
python -m pytest                  # 全部 50+ 用例
python -m pytest -v               # 列出每个用例
python -m pytest tests/test_tree.py        # 只跑树/异常检测
python -m pytest -k retention               # 按关键字挑选
```

覆盖场景：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_model.py` | span 构造、JSON 导入别名、ISO/微秒时间、校验、序列化稳定性 |
| `test_tree.py` | 正常树、**乱序 span**、**孤儿**、**重复 ID**、自环/三节点环、环前导节点 |
| `test_analysis.py` | 总延迟、状态汇总、嵌套不重复累计、**并行子 span** 区间合并、**critical path** 贡献与 parallel_time、空 trace |
| `test_query.py` | 按 service/status/attribute/时间范围/trace_id 过滤、AND/OR 语义、稳定排序 |
| `test_store.py` | 持久化与重开恢复、幂等重投、冲突重复拒绝、**索引损坏自愈与重建**、JSON/JSONL 导入、**retention 清理后查不到、重开后仍生效**、max-traces |
| `test_report.py` | JSON/文本报告确定性、摘要 vs 详情、乱序入库后关联完好、订阅通知 |
| `test_server.py` | 本地 HTTP 收发、批量/信封格式、400 处理、模拟 trace、**独立进程 POST** |
| `test_cli.py` | 在真实终端子进程中跑通 simulate→stats→query→report→retain→reindex 全流程 |

---

## 项目结构

```
.
├── localtrace/
│   ├── model.py       # Span / Status / 时间转换
│   ├── tree.py        # 树重建 + 孤儿/重复/循环检测
│   ├── analysis.py    # 延迟、exclusive 耗时、critical path
│   ├── store.py       # 本地 JSONL 存储、索引、retention、原子写
│   ├── collector.py   # 接收入口（内存 API）
│   ├── query.py       # 过滤器与查询
│   ├── report.py      # 稳定 JSON / 文本报告
│   ├── server.py      # 本地 HTTP 接收端（标准库）
│   ├── simulator.py   # 本地函数/进程模拟服务
│   ├── cli.py         # 命令行
│   └── __main__.py    # python -m localtrace
├── tests/             # pytest 自动化测试
├── examples/spans.json
├── pytest.ini
└── README.md
```
