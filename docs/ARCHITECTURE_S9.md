# RADIANT-LLM 架构（S9，与真实代码一致）

> 每个模块都可在 trace 中找到对应事件。本图以 2026-09-24 工作区代码为准
> （S1–S8 阶段门验收，552 passed）。

## 主链（默认路径）

```text
POST /runs（或 /query knowledge QA，S9-1 统一）
  │  dict{"goal"|"query", "workspace"}
  ▼
RuleRouter            app/control/router.py
  │  RouterDecision(action/intent/confidence/reason_codes)
  │  中英文确定性规则；注入→abstain、低置信→clarify（不建 run）
  ▼
RulePlanner           app/control/planner.py
  │  五步 ExecutionPlan（Pydantic，extra=forbid）：
  │  s1-search → s2-inspect → s3-context → s4-draft → s5-verify
  │  步骤间数据走结构化引用（ADR-0003 StepOutputReference，禁模板 eval）
  ▼
SchemaGuard           app/control/schema_guard.py
  │  未知工具/错参数/引用越权/风险降标 → 422，工具调用 0 次
  ▼
PolicyEngine          app/control/policy.py
  │  workspace ACL/预算/幂等键 → 403 / waiting_review
  ▼
DurableRunner         app/durable/runner.py
  │  StateGraph 合法转移（自研，非 LangGraph）
  │  checkpoint+event+lease(fencing)+幂等+typed retry+租约续约
  │  绑定解析：from_step succeeded checkpoint → resolved 重过 schema
  │  trace: node_*/binding_resolved/run_*
  ▼
工具层（生产默认 real，无 feature flag）
  evidence.search     hybrid BM25+Dense+RRF + metadata/relevance/sufficiency gate
  evidence.inspect    workspace 隔离 + 列级有效期
  context.assemble    ContextPackage 分区预算/quota/pin/dedup/neighbor
  answer.draft        DeepSeek（无 ContextPackage 即拒）
  answer.verify       atomic claims→Claim-Evidence Map→Verifier→一次有界 revise
  ▼
checkpoint/events（durable.db）+ review_queue.db（高风险/冲突人工审核）
  ▼
GET /runs/{id}（答案+claims+citations）/ SSE events（Last-Event-ID 续传）
```

## 旁路控制面

- Memory 治理：`POST/GET/DELETE /memories`（Write Gate 默认拒、Read Gate 隔离）
  + 回答 accept 后自动写 session 摘要/证据指针（S10；user_fact/decision 仍需人工确认），
  供给 context.assemble 的 memory 分区（app/memory/）
- 视觉证据：figure-level（无 bbox，诚实降级），modality quota + Visual Fact Gate
- Eval Harness：`python -m app.eval.runner`（离线 6 层 + agent_e2e 真实主链层），
  Release Gate 接 /benchmarks/run；统一指纹六段
- 旧链：/query（legacy=显式开关）与 /stream-query 保留，非 tool 意图由 Router 明示分流

## 5 分钟 Demo 脚本

```bash
# 0. 启动服务（生产配置）
cd app && ../runtime/bin/python api.py &        # 或 Docker_Executable compose
# 1. 就绪检查（S9-5）
curl -s localhost:8080/health/ready | jq .status
# 2. 提一个带引用要求的 PDF 问题（TEACH-T01）
curl -s -X POST localhost:8080/runs -H 'Content-Type: application/json' -d '{
  "goal": "What two main components does the Transformer architecture consist of? Answer with the PDF name, page number and Evidence ID."}' | jq .
# 3. 看五步链逐步执行（事件流，含 binding_resolved）
curl -s -N "localhost:8080/runs/<run_id>/events" | head -30
# 4. 取验证过的答案与逐 claim verdict
curl -s localhost:8080/runs/<run_id> | jq '.steps[] | {step_id, state, output: (.output|keys?)}'
# 5. 治理演示：写一个未确认的"事实"（应被拒），再写一个确认的偏好（应通过）
curl -s -X POST localhost:8080/memories -H 'Content-Type: application/json' -d '{"category":"user_fact","subject":"x","value":"y","write_reason":"t","confidence":0.5,"provenance":{"origin":"model"}}' | jq .outcome
# 6. 一键全链 smoke
bash deploy/smoke_e2e.sh 8080
# 7. 一键评测（真实主链层）
runtime/bin/python -m app.eval.runner --layer agent_e2e --out artifacts/eval/demo
```
