# tracelet —— 本地分布式 Trace 收集与分析工具

tracelet 是一个**完全本地**的分布式调用链（trace）收集、重建、分析工具。
所有 span、trace、索引、统计信息和报告只存储在**本地文件或进程内存**中：

- 不依赖 Jaeger、Zipkin、OpenTelemetry Collector；
- 不依赖任何数据库或云监控服务；
- 不发起任何网络请求；测试服务全部由本地函数/子进程模拟；
- 仅使用 Python 标准库，`pip install` 零第三方依赖。

## 功能一览

1. **接收/导入 span**：包含 trace id、span id、parent id、service、时间范围、
   状态和 attributes；支持 JSONL / JSON 数组文件导入，时间支持微秒整数和
   ISO-8601。
2. **重建 trace tree**：按 parent-child 关系组装完整树，并检测
   **孤儿 span（orphan）**、**重复 span ID（duplicate）**、
   **循环父子关系（cycle）**。检测到环时会断开形成环的回边，仍生成可用的树。
3. **乱序到达**：ingest 只是追加写入，父子是否已到齐不影响存储；重建在
   reindex 时统一进行，因此父 span 晚于子 span 到达不会丢失关联。
4. **延迟计算**：trace 总延迟、各 service 耗时（同一服务的嵌套/并行 span
   按时间区间**合并去重**，不重复累计）、每个 span 的 self time、critical path。
5. **过滤查询**：按 service、状态、attribute（存在或 key=value）和时间范围
   （区间相交语义）过滤，输出键排序的稳定 JSON 或确定性文本报告。
6. **本地 retention 清理 + 索引重建**：按保留条数（`--keep-last`）或最大年龄
   （`--max-age-us`）删除，同步重写 span/event 日志、删除索引文件并重建；
   清理后查询保证不返回已删除数据。
7. **自动化测试**：覆盖正常树、乱序、孤儿、重复 ID、并行子 span、环、
   critical path、过滤、retention、报告稳定性和 CLI 子进程端到端场景。

## 目录结构

```
tracelet/
  model.py       # Span 模型、时间/状态归一化与校验
  analyzer.py    # 树重建、异常检测、self time、service time、critical path
  storage.py     # 本地文件 / 纯内存存储（JSONL + 每 trace 一个索引）
  collector.py   # 收集器：ingest / import / reindex / query / retention
  query.py       # 过滤器与稳定排序查询
  report.py      # 稳定 JSON 与文本报告
  simulator.py   # 本地函数模拟的多“服务”（edge/checkout/payment/...）
  cli.py         # 命令行入口（python -m tracelet）
tests/           # unittest 测试，全部在终端执行
```

## 快速开始

```bash
# 由本地模拟服务生成 3 条 trace（乱序上报，含一条 ERROR 和一条孤儿 span）
python -m tracelet --data-dir ./trace-data demo --count 3

# 查询：按状态 / 服务 / 属性
python -m tracelet --data-dir ./trace-data list --status ERROR --format json
python -m tracelet --data-dir ./trace-data list --service payment --attr currency=CNY
python -m tracelet --data-dir ./trace-data list --start 1700000000000000 --end 1700000001000000

# 单条 trace 的文本 / JSON 报告
python -m tracelet --data-dir ./trace-data get tr-000
python -m tracelet --data-dir ./trace-data get tr-000 --format json

# 汇总统计
python -m tracelet --data-dir ./trace-data summary

# 索引重建（只从 span 原始日志重算）
python -m tracelet --data-dir ./trace-data reindex

# retention：只保留最近 2 条；或删除早于指定年龄的 trace
python -m tracelet --data-dir ./trace-data retention --keep-last 2
python -m tracelet --data-dir ./trace-data retention --max-age-us 60000000

# 纯内存演示（不落盘）：叶子 span 先于 root 上报
python -m tracelet --memory ooo

# 导入外部文件
python -m tracelet --data-dir ./trace-data import spans.jsonl
python -m tracelet --data-dir ./trace-data import spans.json --format json
```

## Trace 模型

```
Span
  trace_id    : 所属 trace
  span_id     : trace 内唯一
  parent_id   : 父 span；null/"" 表示 root
  service     : 服务/进程名
  name        : 操作名（HTTP 路由、RPC 方法等）
  start_us    : 起始时间，Unix epoch 起的微秒整数
  end_us      : 结束时间（end_us >= start_us，否则记录被拒绝）
  status      : "OK" | "ERROR"（兼容 SUCCESS / FAIL / OTLP 数字码）
  attributes  : 扁平的 key -> str|int|float|bool
```

一个 trace 由若干 span 构成，每个节点至多有一个父节点（functional graph）。
重建时：

- 先按 trace_id 分组，同 `(trace_id, span_id)` 的重复上报只保留**第一条**，
  其余记为 `duplicate_span_id` 异常（收集器层面对已存储 ID 的再次上报直接
  计为 duplicate，不写日志）；
- `parent_id` 指向不存在 span 的节点成为 root，同时记 `orphan_span_id`；
- 沿父指针做链检测（每节点出度 ≤1）：再次走入当前链上的节点即为环，
  断开“闭合环的那条回边”，每个不同的环恰好报一个 `cycle` 异常，
  自环（parent==self）同样支持。

### 时间计算

- **trace 总延迟** = `max(end_us) - min(start_us)`。
- **span self time** = 自身时长 − 被直接子 span 覆盖部分的**区间并集**；
  子 span 区间会先裁剪到父 span 自身区间内（容忍时钟漂移/脏数据），
  self time 不会为负。
- **service 耗时** = 该服务所有 span `[start, end)` 区间合并后的总长度。
  嵌套或并行的同服务 span 共享的时间段只算一次。
  例：父 10..60，子 20..30、40..50 → service time = 50，而不是 70。

### Critical path 算法

节点权重使用 **self time（排他时间）**，critical path 是权重和最大的
root→leaf 路径。后序遍历计算每个节点：

```
crit_to_leaf(node) = self(node) + max(crit_to_leaf(child))
```

之后从 `crit_to_leaf` 最大的 root 开始沿 `crit_child` 下行即可。
因此：

- **并行子 span 不会重复累计**（它们只在各自的候选分支里贡献一次）；
- 嵌套 span 不会把父被子覆盖的时间重复算进路径权重；
- 报告中的 `critical_path_us` 是路径上 self time 之和（真实路径权重），
  `critical_path_span_us` 仅作为沿路径各 span 墙钟时长之和的参考值。

叶子权重相同等平局情况按 `(start_us, span_id)` 确定性打破，保证输出稳定。

## 文件格式（全部本地、人类可读）

```
<data-dir>/
  spans.jsonl           # 已接收 span，每行一个 JSON（sort_keys）
  events.jsonl         # reindex 产出的异常事件，每行一个 JSON
  index/
    tr_<traceid>.json   # 每条 trace 一份完整分析结果（含树、critical path）
    summary.json        # 跨 trace 统计（计数、分位延迟、服务分布、异常计数）
```

- 所有写操作都是 **临时文件 + 原子 rename**，进程被杀死也不会留下半个文件。
- trace id 作为文件名时只允许 `[A-Za-z0-9_.-]`，其它字符使用
  `tr_<hex>.json` 编码，任意客户端 ID 都能安全落盘。
- `reindex` 完全由 `spans.jsonl` 重算：清空 `index/`、重建每棵树与 summary，
  可用于修复/补建索引。
- `retention` 先过滤重写 `spans.jsonl`/`events.jsonl`、删除被清理 trace 的
  索引文件，再执行 reindex；内存存储（`LocalStore(None)` 或 `--memory`）
  语义一致，只是不接触磁盘。

单条 trace 索引 JSON 的主要字段：

```json
{
  "trace_id": "...",
  "start_us": 0, "end_us": 100, "duration_us": 100,
  "status": "OK", "error_count": 0, "span_count": 3,
  "service_time_us": {"svc-a": 50},
  "critical_path": [ {span...}, ... ],
  "critical_path_us": 60,
  "critical_path_span_us": 90,
  "roots": [ {"span": {...}, "self_us": 20, "children": [...]} ],
  "anomalies": [ {"kind": "orphan_span", "span_id": "...", "message": "..."} ]
}
```

## 作为库使用

```python
from tracelet import Collector, LocalStore
from tracelet.query import make_filter

c = Collector(LocalStore("./trace-data"))
c.ingest_many_dicts([
    {"trace_id": "t1", "span_id": "child", "parent_id": "root",
     "service": "svc", "start_us": 10, "end_us": 30},
    {"trace_id": "t1", "span_id": "root", "parent_id": None,
     "service": "gw",  "start_us": 0,  "end_us": 100},   # 父 span 后到
])
c.reindex()
for t in c.query(make_filter(service="svc", status="OK")):
    print(t["trace_id"], t["critical_path_us"])
c.retention(keep_last=10)
```

## 运行测试

所有测试基于标准库 `unittest`，在终端执行，无需任何外部服务：

```bash
python -m unittest discover -s tests -v
```

覆盖场景：正常树结构与总延迟、乱序（父晚到）、孤儿、重复 ID、
二节点/三节点/自环/带外链入边的环、并行子 span 区间合并、子 span 超出父
区间裁剪、critical path 选路与权重、service/status/attribute/时间范围过滤、
文件持久化与重开重建、JSONL 导入、retention 清理后数据不可查、报告稳定性、
以及通过真实子进程执行 CLI 的端到端生命周期测试。
