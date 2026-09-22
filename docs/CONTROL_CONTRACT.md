# RADIANT-Control — Agent Control Plane 契约（M2）

状态：M2 骨架，仅 mock tools。控制层尚未接入 `convchain_api` / `AgentExecutor`（后续 Milestone 集成）。本文档是 Router / Planner / Schema Guard / Policy Engine / Scheduler 之间的冻结契约说明。

## 1. 链路

```text
query → Router (RouterDecision)
      → Planner (仅提出 ExecutionPlan，无工具执行权)
      → Schema Guard (工具名/参数/DAG/预算/风险一致性)
      → Policy Engine (workspace ACL/风险等级/幂等要求/预算余量 → allow|deny|review)
      → Scheduler (顺序执行，拓扑排序，运行时预算强制)
      → RunSummary (ToolResult + provenance 汇总)
```

Planner 的输出只能以原始 JSON 形式进入 Schema Guard，不得直接触达执行器；Planner 只拿到 registry 的元数据视图（`ToolRegistry.catalog()`，不含 handler）。

## 2. 数据契约（`app/control/models.py`，pydantic v2，全部 `extra="forbid"`）

- **RouterDecision**: `intent: knowledge_qa|visual_qa|compare|ingest|audit`，`action: respond|tool_call|clarify|abstain`，`confidence: float ∈ [0,1]`，`working_subject: str|null`，`reason_codes: [str]`（非空），`response_contract: {format: answer|table|report|trace, citation_required: bool}`。
- **ExecutionPlan**: `run_id: uuid`，`goal: str`，`steps: [{step_id, tool, arguments: {}, depends_on: [], risk: read_only|bounded_write|external, timeout_ms: int>0, retry_policy: transient_only|none, idempotency_key: str|null}]`，`budgets: {max_tokens, max_tool_calls, max_wall_time_ms}`（均 >0）。
- **ToolResult**: `status: success|retryable_error|terminal_error|denied`，`output: {}`，`artifacts: []`，`error: {code, message, retryable}|null`，`metrics: {latency_ms, token_count}`，`provenance: {tool_version, config_fingerprint}`。
- **ToolSpec**: 注册表条目（name/version/risk/high_risk/arguments_schema/implemented）。`handler` 仅存在于进程内实现的工具（M2 仅 4 个 mock），且被排除在序列化之外。
- **response_contract 解耦**：该字段只存在于 RouterDecision；ExecutionPlan 无此字段，Policy Engine 的 `authorize(plan, workspace)` 签名不接触它。回答格式要求永远不会改变工具授权结果（`tests/control/test_response_contract.py`）。

## 3. Reason Code 表（稳定字符串，deny/clarify/abstain 必须至少携带一个）

| Code | 含义 |
|---|---|
| `router.keyword_match` / `router.default_intent` | 规则命中 / 回退默认意图 |
| `router.low_confidence` | confidence 低于阈值 → clarify |
| `router.empty_input` | 空输入 → clarify |
| `router.injection_suspected` | 疑似 prompt injection（含经 skills 注入变体）→ abstain |
| `router.error:<Exc>` | Router 异常 → fail-closed clarify |
| `planner.error:<Exc>` | Planner 抛异常 → 拒绝，零执行 |
| `planner.invalid_output` | Planner 输出无法通过 ExecutionPlan 校验（含非正预算等）→ 拒绝，零执行 |
| `guard.unknown_tool:<tool>` | 工具名未注册（hallucinated tool） |
| `guard.missing_argument:<key>` | 缺少必填参数 |
| `guard.argument_type_mismatch:<key>` | 参数类型错误（bool 不算 integer） |
| `guard.unexpected_argument:<key>` | 多余/潜在敏感参数（schema `additionalProperties: false`） |
| `guard.cyclic_dependency` | depends_on 成环 |
| `guard.unknown_dependency:<a->b>` | 依赖不存在的 step |
| `guard.duplicate_step_id:<id>` | step_id 重复 |
| `guard.budget_not_positive:<field>` | 预算非正（防御性；契约层通常已拦截） |
| `guard.budget_exceeds_cap:<field>><cap>` | 声明预算超过硬上限（tokens 100k / tool_calls 10 / wall_time 120s） |
| `guard.too_many_steps:<n>><max>` | step 数超过 max_tool_calls |
| `guard.risk_mismatch:<step>(a!=b)` | 计划声明风险与注册风险不一致（禁止自我降权） |
| `policy.allowed` | 放行 |
| `policy.workspace_denied` | 工具不在 workspace ACL（含未登记 workspace → 全拒） |
| `policy.missing_idempotency_key` | bounded_write 工具缺 idempotency_key |
| `policy.external_review:<tool>` / `policy.high_risk_review:<tool>` | external / 高风险工具 → review，不执行 |
| `policy.budget_exceeded` | 计划预算超过 workspace 剩余额度 |
| `tool.not_implemented` | 元数据登记的 legacy 工具在 M2 不可执行 |
| `tool.execution_error` / `runtime.budget_exhausted` | 工具异常 / 运行时预算耗尽（剩余 step 记 denied） |

## 4. Fail-closed 语义

- Planner 抛异常或输出非法 JSON/不符契约 → `rejected`，执行工具数恒为 0（`tests/control/test_fail_closed.py`）。
- Router 异常 → `clarified`，不进入规划。
- Guard 或 Policy 任一不通过 → 整个计划不执行（不做部分执行）。
- 未登记的 workspace：ACL 为空集、预算余量为 0 → 必然 deny。
- 工具自身异常被 registry 包装为 `terminal_error`，不向上抛原始异常。
- 副作用工具有两道幂等防线：Policy 要求计划携带 `idempotency_key`；`report.export` 工具内部对缺 key 也直接 `denied`。

## 5. 工具风险登记（17 = 4 mock + 13 legacy 元数据）

| 工具 | 风险 | 高风险标记 | M2 可执行 |
|---|---|---|---|
| evidence.search / evidence.inspect / citation.validate | read_only | 否 | 是（mock 0.1.0-mock） |
| report.export | bounded_write | 否 | 是（mock，幂等账本） |
| PDFReaderTool, URLValidationTool, ImageAnalysisTool, CSVandExcelFileParserTool, CSVDataFinderTool, TextFileReaderTool, SkillLookupTool | read_only | 否 | 否（元数据 1.0.0-legacy） |
| PDFKnowledgeBaseSanitizerTool | bounded_write | 否 | 否 |
| WebScraperTool, WikipediaSearchTool | external | 否 | 否 |
| WebSearchTool, PythonREPLTool, FileDownloaderTool | external | **是** | 否 |

external 或 high_risk 工具：Policy 一律 `review`，未经人工批准不执行。legacy 13 工具在 M2 只登记元数据，不 import、不实例化。

## 6. Workspace ACL（M2 骨架值）

- `default` / `lowbudget`：4 个 mock；`readonly`：仅 3 个只读 mock；`full`：全部 17 个注册工具（external/高风险仍需 review，legacy 仍不可执行）。
- `lowbudget` 在测试台账中额度 1000 tokens，用于冻结预算越界用例。

## 7. 冻结契约测试集与措辞红线

- 冻结用例集：`benchmarks/router_cases.jsonl`（12 例）、`benchmarks/policy_cases.jsonl`（16 例），每条含 `case_id / input / expected / risk / source`，pytest parametrized 执行（`tests/control/`，全离线、无 LLM）。
- **措辞红线**：验收门中的"非法计划拦截率 100%""未授权执行 0"**仅限上述冻结契约测试集**，用于证明控制逻辑独立于模型能力；不得泛化为"现实系统绝对安全""对所有输入 100% 拦截"之类的表述。任何对外描述必须带"冻结契约测试集"限定语。

## 8. 系统提示词中的策略性条款（Policy 规则候选登记）

以下来自 `app/system_prompt_radiant_llm.yml` 的策略性条款，M2 登记为候选，逐条决定保留在提示词还是迁入代码（M2 暂全部保留在提示词，不在本骨架内强制）：

1. **Web-Search Decision Rule**（第 42 行起：默认不用 web 工具，仅特定条件启用）→ 候选迁入代码：与 `WebSearchTool/WebScraperTool external + review` 策略对应，建议 M4+ 作为 Policy 规则落地。
2. **FileDownloader 显式确认条款**（第 92 行：下载前必须解释并获用户明确同意）→ 候选迁入代码：映射为 `FileDownloaderTool` review 门禁 + 审批事件。
3. **Skill Invocation Policy**（第 59 行起：bundled 安全策略优先、skill 失败才允许 web fallback）→ 候选保留提示词 + 代码双轨：Router 已含 skills 注入变体的 injection 检测；skill 白名单治理待 Memory/Skills Milestone。
4. **Policy-Hidden Reasoning 拒答条款**（第 368 行起）→ 保留在提示词，属于回答层策略，不涉及工具授权。
