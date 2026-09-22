# RADIANT-Control M9: HTTP API 契约

本文档覆盖 M9 新增的控制平面 API（runs / reviews / benchmarks / ingest / dashboard）。
既有对话 API（`/query`、`/stream-query`、`/sessions`、`/evidence`、`/documents` 等）见代码 `app/api.py`
与 OpenAPI 页面 `/docs`。

约定：

- 所有错误都是结构化 JSON：`{"detail": ...}`；`detail` 为字符串，或在计划/策略拒绝时为对象
  `{"code": ..., "reason_codes": [...], ...}`。
- 后台工作（run 执行、benchmark、ingest、review 触发的 resume）都在 daemon 线程/子进程中，
  创建类端点返回 `202` 与任务 id，状态用对应 GET 轮询或 SSE 订阅。
- 控制平面数据库路径：`RADIANT_DURABLE_DB` / `RADIANT_REVIEW_DB` / `RADIANT_EVIDENCE_DB` 环境变量优先，
  其次 `RADIANT_LLM_CONFIG_DIR`，缺省仓库根目录下的 `durable.db` / `review_queue.db` / `evidence.db`。

## Runs

### POST /runs

创建持久化 run：`router → planner → Schema Guard → Policy Engine → DurableRunner（后台执行）`。

Request:

```json
{
  "goal": "search evidence and export report",   // 或 "query"，二者等价，必填
  "workspace": "default",                         // 可选，默认 default
  "budgets": {"max_tokens": 5000, "max_tool_calls": 4, "max_wall_time_ms": 30000}  // 可选覆盖
}
```

Responses:

- `202`：已创建，后台执行中。

  ```json
  {"run_id": "…", "status": "running", "goal": "…", "workspace": "default",
   "policy_verdict": "allow", "reason_codes": ["policy.allowed"],
   "config_fingerprint": "…",
   "steps": [{"step_id": "s1-search", "tool": "evidence.search", "risk": "read_only",
              "depends_on": [], "retry_policy": "transient_only", "timeout_ms": 5000}]}
  ```

- `200` 且 `run_id: null`：router 未选择工具调用，`status` 为 `respond | clarify | abstain`
  （低置信度 → clarify；注入嫌疑 → abstain），不创建 run。
- `400`：缺 goal / budgets 非法。
- `403`：Policy 拒绝。`detail.code = "policy_denied"`，`reason_codes` 如 `policy.workspace_denied`、
  `policy.missing_idempotency_key`、`policy.budget_exceeded`。
- `422`：计划非法。`detail.code = "plan_rejected"`，`reason_codes` 为 guard.* 系列
  （如 `guard.budget_exceeds_cap`）。

### GET /runs

run 列表（按创建时间倒序，只读视图）。Query：`limit`（1–1000，默认 100）、`offset`。

```json
{"total": 3, "limit": 100, "offset": 0,
 "items": [{"run_id": "…", "goal": "…", "workspace": "default", "state": "succeeded",
            "owner": "api-worker", "config_fingerprint": "…",
            "cancel_requested": false, "created_at": 1758…, "updated_at": 1758…}]}
```

### GET /runs/{run_id}

状态图快照。`404 {"detail": "run '…' not found"}` 当 run 不存在。

```json
{"run_id": "…", "goal": "…", "workspace": "default", "state": "running",
 "owner": "api-worker", "config_fingerprint": "…", "cancel_requested": false,
 "created_at": …, "updated_at": …, "latest_event_seq": 6,
 "steps": [{"step_id": "s1-search", "tool": "evidence.search", "risk": "read_only",
            "depends_on": [], "state": "succeeded", "attempt": 1,
            "error": null, "artifacts": [], "output": {…}}]}
```

`state` ∈ `pending | running | succeeded | failed | waiting_review | cancelled`；
节点 `state` ∈ `pending | running | succeeded | failed | waiting_review | cancelled`。

### GET /runs/{run_id}/events （SSE）

`text/event-stream`。每条事件：

```
id: <seq>
event: <event_type>
data: {"run_id": "…", "created_at": …, ...payload}
```

- `id` 即事件在事件日志中的 `seq`（单调递增）。
- 断线重连：请求头 `Last-Event-ID: <已收到的最大 id>`，服务端从 `seq > Last-Event-ID` 续推，无gap。
- run 进入 `succeeded | failed | cancelled` 后服务端排空事件并发送 `event: end` 关闭流；
  `waiting_review` 期间流保持打开（review 决策后会继续出事件）。
- 每 15s 一行 `: keep-alive` 心跳；单次连接最长 30 分钟。

事件类型（`app.durable.events.EventType`）：

| event | 含义 | payload 关键字段 |
|---|---|---|
| `run_started` | run 开始 | goal, workspace, owner, config_fingerprint, fencing_token |
| `node_started` | 节点开始 | step_id, tool |
| `node_completed` | 节点成功 | step_id, attempt, latency_ms |
| `node_failed` | 节点失败（不再重试） | step_id, attempts, status, code, classification |
| `node_retried` | 节点重试 | step_id, attempt, delay_ms, code |
| `node_skipped` | 命中 checkpoint 跳过 | step_id, reason, checkpoint_attempt |
| `waiting_review` | 节点暂停待人工审核 | step_id, tool, risk |
| `run_resumed` | run 从 checkpoint 恢复 | restored_steps, config_fingerprint, fencing_token |
| `run_completed` | run 成功 | tools_executed, resumed |
| `run_failed` | run 失败 | step_id, status, code |
| `run_cancelled` | run 取消 | cancelled_steps |
| `lease_acquired` / `lease_taken_over` | 租约获取/接管 | owner, fencing_token, expires_at |

### POST /runs/{run_id}/cancel

协作式取消（持久标志 + 内存 token，节点间与节点执行中都生效）。

- `200`：`{"status": "cancel_requested"}`；已终态则返回当前状态与 `"detail": "run already terminal"`。
- `404`：run 不存在。

### POST /runs/{run_id}/resume

从 checkpoint 恢复（已完成节点不重复执行；配置指纹不匹配则拒绝）。

- `200`：`{"status": "resuming"}`（后台执行）。
- `404`：run 不存在。`409`：run 已终态，或 plan 不在本进程内存中
  （进程重启后请通过 review item 的 decision 端点恢复，plan_json 已持久化）。
- 注意：对 `waiting_review` 的 run 直接 resume 而未做 review 决策，会在同一节点再次暂停。

## Reviews（人工审核队列）

外部/高风险工具的节点会把 run 暂停到 `waiting_review` 并自动入队一个 review item。

### GET /reviews

- 默认：待审队列。Query `run_id`：该 run 的全部 item（含已决策）。
- item 字段：`review_id, run_id, status, claims, evidence_snapshot, risk_reasons, workspace,
  metadata, decision, reviewer_id, rationale, created_at, decided_at`。
  **`plan_json` 不在列表/详情响应中返回**（内部恢复材料）。

### POST /reviews/{review_id}/decision

```json
{"decision": "approve | reject | edit",
 "reviewer_id": "operator-1",           // 必填
 "rationale": "…",                       // 决策理由
 "edited_answer": "…"}                   // 仅 decision=edit 时必填
```

- `200`：`{"status": "decided", "resume": "started", "item": {…}}`。
  approve/edit → 后台恢复**原 run**（被审节点的 gate 对该次决策清除）；
  reject → 置取消标志后 resume，run 以 `cancelled` 终态并留完整事件轨迹。
- `400`：decision 非法或缺 reviewer_id。`404`：review 不存在。
- `409`：重复决策（决策不可变）或 edit 缺 edited_answer。

## Benchmarks（M8 评测回归）

### POST /benchmarks/run

```json
{"layers": ["control", "durable"],   // 可选；缺省=全部注册层
 "baseline": "artifacts/eval/m8-baseline-20260922"}  // 可选；缺省即该基线
```

- `202`：`{"benchmark_id": "m9-<ts>-<hex>", "status": "running", "layers": [...], "out_dir": "…"}`。
  后台以子进程跑 `eval.runner`，日志在 `<out_dir>/runner.log`，状态持久化到 `<out_dir>/job.json`。
- `400`：layers 为空列表 / baseline 路径不存在。`422`：未知 layer
  （`detail.known` 给出可选值：`control, durable, retrieval, context, memory, verification`）。

### GET /benchmarks/{benchmark_id}

```json
{"benchmark_id": "…", "status": "running | done | failed", "exit_code": 0,
 "started_at": "…", "finished_at": "…",
 "report": {"run_id": "…", "summary": {"layers_ok": …, "cases_total": …, "cases_failed": …},
            "metrics_flat": {"control.accuracy": 1.0, …},
            "baseline_comparison": {"baseline_run_id": "m8-baseline-20260922",
                                    "metrics": {"<key>": {"baseline": …, "current": …, "delta": …}}},
            "failed_cases": [{"layer": "…", "case_id": "…", "dataset": "…", "error": "…"}]}}
```

`report` 在评测写出 `report.json` 前为 `null`。`404`：id 不存在。

### GET /benchmarks

本进程触发的任务 + `artifacts/eval/` 下所有含 `report.json` 的历史 run（含 M8 基线）的列表。

## Documents ingest

### POST /documents/ingest

```json
{"input_dir": "/path/to/dir",     // 必填，须存在
 "workspace_id": "default",        // 可选
 "parse": true,                    // 可选：无 KB JSONL 且含 PDF 时先跑解析管线
 "text_mode": "lightweight",       // 可选：lightweight | nougat
 "vision_model": "deepseek-flash", // 可选：视觉解析模型
 "source_dir": "/path/to/pdfs"}    // 可选：原始 PDF 目录（artifact 溯源）
}
```

- `202`：`{"job_id": "ing-…", "status": "queued"}`。
- `400`：缺 input_dir / 目录不存在 / text_mode 非法。
- input_dir 已含 `01_chunks_kb.jsonl` 等 KB 输出时直接幂等摄取（content_hash + parser 指纹短路）。

### GET /documents/ingest/{job_id}

```json
{"job_id": "…", "status": "queued | running | done | failed",
 "phase": "parse | ingest | null", "input_dir": "…", "workspace_id": "…",
 "result": {"documents": [...], "new_records": 12, "failed": [], "rejected_rows": 0},
 "error": null, "created_at": "…", "started_at": "…", "finished_at": "…"}
```

`404`：job 不存在（job 状态保存在进程内存，重启后不可查）。

## Dashboard

- `GET /dashboard/`：运维 Dashboard（原生 JS 静态页，`app/dashboard-static/`，无构建步骤）。
  页面：首页（系统状态+快捷入口+摄取）、Runs（列表/详情/事件流/取消/恢复）、
  Evidence Inspector、Review 队列决策、Benchmarks（触发+基线对比+失败标红）。
- React 对话前端仍在 `/`（catch-all 注册在最后，不会吞掉以上 API 的 404）。

## 错误码总览

| HTTP | 场景 |
|---|---|
| 400 | 缺字段 / 参数非法 / baseline 不存在 / input_dir 不存在 |
| 403 | Policy 拒绝（`policy_denied` + reason_codes） |
| 404 | run / review / benchmark / ingest job / evidence 不存在（结构化 JSON） |
| 409 | run 已终态仍 resume / review 重复决策 / edit 缺 edited_answer / plan 不在内存 |
| 422 | 计划未过 Schema Guard / 未知 benchmark layer |
