# 首个 Vertical Slice 数据契约（S1-1 冻结）

> 冻结日期：2026-09-23。适用范围：只读 PDF 知识问答 slice（计划 §5）。
> 本文件与 `tests/control/test_slice1_contract.py` 中的样例一一对应，任一侧修改必须同 commit 同步。
> 修改契约 = 显式决策：先提 ADR 或在任务卡 T2 中声明，禁止悄悄改字段。

## 0. 五份契约的交接顺序

```text
QueryRequest → RouterDecision → ExecutionPlan → ToolResult →（引用）Evidence
   用户生产      Router 生产      Planner 生产     工具生产      摄取链生产
   API 消费       Planner 消费     Guard/Policy/   Runner/答案    search/inspect
                                   Runner 消费      消费          与答案消费
```

全部模型 `extra="forbid"`：契约未声明的字段在校验时直接拒绝。

## 1. QueryRequest（本卡新增，`app/control/models.py`）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| question | str | min_length=1 | 问题本体 |
| workspace_id | str | 默认 "default" | 隔离边界，参与 evidence_id 哈希 |
| session_id | str \| null | 可选 | 会话指针，S5 记忆治理用 |

```json
{"question": "What two main components does the Transformer architecture consist of? Answer with the PDF name, page number and Evidence ID.", "workspace_id": "default", "session_id": null}
```

## 2. RouterDecision（已存在，`app/control/models.py:126`）

```json
{"intent": "knowledge_qa", "action": "tool_call", "confidence": 0.85, "working_subject": null, "reason_codes": ["router.keyword_match"], "response_contract": {"format": "answer", "citation_required": true}}
```

## 3. ExecutionPlan（已存在，`app/control/models.py:158`）

slice 目标形态为两步 search→inspect：

```json
{"run_id": "<uuid4>", "goal": "<question>",
 "steps": [
  {"step_id": "s1-search", "tool": "evidence.search", "arguments": {"query": "<question>", "top_k": 3, "workspace_id": "default"}, "depends_on": [], "risk": "read_only", "timeout_ms": 5000, "retry_policy": "transient_only"},
  {"step_id": "s2-inspect", "tool": "evidence.inspect", "arguments": {"evidence_ids": ["ev-1467122274c2347e3ed99f4c"]}, "depends_on": ["s1-search"], "risk": "read_only", "timeout_ms": 5000, "retry_policy": "transient_only"}],
 "budgets": {"max_tokens": 8000, "max_tool_calls": 4, "max_wall_time_ms": 30000}}
```

## 4. ToolResult（已存在，`app/control/models.py:189`）

```json
{"status": "success",
 "output": {"hits": [{"evidence_id": "ev-1467122274c2347e3ed99f4c", "document": "attention_is_all_you_need_1706.03762.pdf", "page": 1, "score": 0.83}], "mock": false},
 "artifacts": [], "error": null,
 "metrics": {"latency_ms": 42, "token_count": 0},
 "provenance": {"tool_version": "evidence.search/2", "config_fingerprint": "sha256:..."}}
```

S1-3 验收要求：`output.mock` 必须为 `false`，`hits[].evidence_id` 必须能在 evidence.db 中查到。

## 5. Evidence（已存在，`app/evidence/models.py:96`，身份规则见 ADR-0002）

```json
{"evidence_id": "ev-1467122274c2347e3ed99f4c", "workspace_id": "default", "document_id": "e8a365c1d8815226", "document_version": "v-891411cca8dc-e56f6c", "content_hash": "891411cca8dcb4c23161bd8f8865631f777285cfc1f04f5cb1da292837258cb4", "page": 1, "section": null, "figure_id": null, "region_bbox": null, "modality": "text", "source_span": {"chunk_id": "e8a365c1d8815226:p1:c2", "prev_chunk_id": "e8a365c1d8815226:p1:c1", "next_chunk_id": "e8a365c1d8815226:p1:c3"}, "artifact_uri": "file:///.../attention_is_all_you_need_1706.03762.pdf", "parser_fingerprint": "evidence-adapter/1;extractor=nougat;vlm=deepseek-flash", "authority_level": "primary", "valid_from": "2026-09-22T11:18:46Z", "valid_to": null, "degraded": false, "content": "<正文>", "metadata_fields": {}}
```

身份规则（摘要）：`evidence_id = sha256(workspace_id | document_version | locator)` 前 24 位；`document_version` 由 `content_hash` + `parser_fingerprint` 派生；旧版本永不覆盖，只标记 `valid_to`。

## 6. 失败判法（与 benchmarks/teaching_case.jsonl 一致）

无 evidence_id（FM-1）、页码错误（FM-2）、编造 ID（FM-3）、事实缺失（FM-4）、引用错文档（FM-5）、mock 混入（FM-6）均判 fail。
