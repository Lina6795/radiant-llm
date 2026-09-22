# FAILURE_RECOVERY — RADIANT-Control M3 恢复语义

组件：`app/durable/`（checkpoint.py / events.py / retry.py / idempotency.py /
lease.py / runner.py）。存储默认 SQLite（WAL），所有 store 的连接均参数化
（`connection_factory` / DSN），SQL 保持可移植子集，可替换 PostgreSQL；
`postgresql://` DSN 当前显式 `NotImplementedError`，不会静默降级。

## 快照与事件分离

- **CheckpointStore**（可变快照）：`runs` 表（run 状态、owner、配置指纹、
  cancel 标志）+ `checkpoints` 表（每 `(run_id, step_id)` 一行，节点每次
  状态变化 upsert：开始写 `running`，完成写 `succeeded/failed`）。
- **EventStore**（append-only）：`events` 表，`(run_id, seq)` 主键，seq 每
  run 单调递增。事件类型：`run_started / node_started / node_completed /
  node_failed / node_retried / node_skipped / waiting_review / run_completed /
  run_failed / run_cancelled / run_resumed / lease_acquired / lease_taken_over`。
  `stream(run_id, after_seq=...)` 即未来 SSE 的读取接口（M3 不接 api.py）。

## Checkpoint 内容

每条节点快照：`run_id, step_id, state, attempt, output(+artifacts), error,
config_fingerprint, created_at`。

`config_fingerprint` = sha256(plan 序列化 + registry 工具目录指纹) 前 16 位，
run 启动时写入 `runs` 表，每条节点快照也携带——恢复时能说明"以哪个配置继续"。

## Resume 算法

1. `get_run(run_id)` 读 run 记录（不存在 → `RunNotFoundError`，fail closed）。
2. 校验配置指纹：与库存指纹不一致 → `CheckpointMismatchError`
   （可显式 `allow_config_mismatch=True` 覆盖，报告中仍同时记录两个指纹）。
3. 终态 run（succeeded/cancelled）直接返回恢复报告，零工具调用。
4. 获取租约（同 owner 续租 / 旧租约过期自动接管 / `force_takeover`），
   fencing token 递增。
5. `StateGraph.restore()`：仅 `succeeded` 节点保留，其余归一化 `pending`。
6. 写 `run_resumed` 事件（携带 `restored_steps`、`from_checkpoints` 的
   `{step_id: attempt}`、两个配置指纹、owner、fencing_token）。
7. 拓扑续跑：有 `succeeded` checkpoint 的节点跳过（写 `node_skipped`），
   其余正常执行。**已完成节点不重复执行**（测试断言调用计数恒为 1）。

## 错误分类与重试

`app/durable/retry.py`：只有 `retryable_error` 可重试；`terminal_error` 与
`denied` 永不重试（重试 deny 等于绕过策略）。指数退避
`base * multiplier^(attempt-1)`，上限 `max_delay_ms`，clock 注入
（测试用 FakeClock，离线零真实 sleep）。节点级 `timeout_ms`：工具在独立
daemon 线程执行，超时返回 `retryable_error(code=node.timeout)`，由重试策略裁决。

## 幂等（副作用恰好一次）

`PersistentIdempotencyLedger`（SQLite）：`idempotency_key → (result, effect_count)`。
`execute_once(key, effect)`：已存在 → 返回首次结果且不调用 `effect`
（网络断开也不受影响，因为副作用函数根本不会被调用）；不存在 → 在 store 锁内
执行副作用并落库。DurableRunner 通过实例属性注入替换 M2 内存账本
（`registry.ledger = persistent`，不改 `app/control/`），`report.export`
在 durable 路径上自动获得跨进程幂等。重复提交（不同 run、相同 key）
`effect_count` 恒为 1。

## Lease 与 fencing

`leases` 表：`(run_id, owner, fencing_token, expires_at)`。

- 获取：无租约 → token=1；同 owner → 续租 token+1；异 owner 未过期 →
  `LeaseConflictError`；异 owner 已过期（或 force）→ 接管 token+1。
- **fencing**：runner 在每个节点提交前后 `validate(owner, fencing_token)`，
  过期或 token 不匹配 → `LeaseFencingError`；被 fence 的 worker 不再写任何
  run 状态（事件/快照/状态全部停笔），由新 owner 单点续跑。
- 时钟注入，测试用 FakeClock 前进虚拟时间制造过期，无需真实等待。

## Cancel 语义（协作式）

`cancel(run_id)` 同时：① 置 `runs.cancel_requested=1`（持久化，跨进程可见）；
② set 该 run 的 `CancelToken`（内存，执行线程可见）。runner 在节点边界
检查两处标志；节点执行期间 invoke 循环每 5ms 轮询 token，token 感知型工具
可经 `runner.cancel_token(run_id)` 自行轮询。命中后：当前及剩余节点迁移到
`cancelled`，写 `run_cancelled` 事件，**不再产生任何新工具调用**
（测试断言 cancel 时刻之后 invoke 计数为零增长）。

## 恢复成功率

冻结用例 `benchmarks/runtime_cases.jsonl`（RT-01..RT-08，对应故障注入 a–h），
`tests/durable/test_runtime_cases.py` 参数化执行，逐 case 对比 `expected`
块，模块结束时打印：

```
RUNTIME_CASES_SUMMARY {"total_cases": 8, "recovered_cases": 8, "recovery_success_rate": 1.0, ...}
```

当前值：**8/8 = 1.0**。
