# M7: Claim Verification, Visual Evidence QA 与 Human Review

状态：2026-09-22 完成（框架 + 冻结集 B0–B3 实测）。视觉真实实验 **deferred**（VLM key 未到位）。

## 1. 契约

### 1.1 原子 Claim（`app/verification/claims.py`）

- `Claim{claim_id, text, claim_type, sentence_index, numbers, units, entities}`。
- `claim_type ∈ {numeric, unit, factual, relational, visual}`。
- 拆分默认走确定性规则版（句切分 + 数字/单位/关系词/视觉词分类）；`split_claims(answer, llm_callable=...)` 可注入 LLM，任何异常/非法输出 **显式回退** 到规则版（`splitter="llm_fallback"`，detail 记录原因），绝不静默产出畸形 claim。
- `claim_id = cl-<sha1(规范化文本)[:12]>`：同一答案拆分结果逐位稳定（测试锁定）。

### 1.2 Claim-Evidence Map（`app/verification/claim_map.py`）

- 每个 claim 绑定 `{evidence_id, page, document_id, confidence, reason, status}`，
  `status ∈ {supported, unsupported, conflicted}`。
- 评分：内容词 Jaccard overlap + 数字命中加成；degraded 证据永不参与绑定（沿用 M1 语义）。
- 数字/单位类 claim 要求 **全部** 数字在证据原文出现（逗号归一化：`100,000` ≡ `100000`）；
  缺失即 unsupported 并列出缺失值。
- 冲突检测：最佳证据确认 claim 关键数字，但第二高 overlap 证据缺失该数字且携带双方都没有的
  新数字（同一数值槽位不同取值）→ `conflicted`。

### 1.3 VerificationDecision（`app/verification/verifier.py`）

```
action ∈ {commit, retrieve_more, clarify, human_review, abstain}
+ supported / unsupported / citation_gaps / conflicts (claim_id 列表)
+ reason_codes (verify.* 稳定字符串) + coverage + numeric_accuracy + 逐 claim checks
```

检查项：数字一致性、单位一致性、适用条件（claim 实体须出现在绑定证据中，防"数字对但条件错"）、
来源冲突、citation coverage、authority 等级。

升级策略（顺序）：

1. 硬失败（冲突 / 数字/单位/条件不匹配 / 低 authority）→ `human_review`
2. 矛盾（claim 绑到高 overlap 证据但数字缺失）→ `human_review`
3. 高风险 case 且存在 unsupported → `human_review`
4. coverage < 阈值（默认 0.7）→ `retrieve_more`（实验内触发一次证据池扩展 5→10 并复检，不重新生成）
5. 其余 unsupported → `human_review`；全部通过 → `commit`
6. 无 claim → `abstain(verify.no_claims)`；无证据 → `abstain(verify.no_evidence)`

### 1.4 Human Review Queue（`app/verification/review.py`）

- SQLite，路径解析：显式参数 > `RADIANT_REVIEW_DB` > `RADIANT_LLM_CONFIG_DIR/review_queue.db` > `./review_queue.db`。
- `enqueue(run_id, claims, evidence_snapshot, risk_reasons, plan, ...)`：存序列化 M2 `ExecutionPlan`。
- `decide(approve|reject|edit, reviewer_id, rationale, edited_answer?)`：**决策不可变**，二次 decide 抛错。
- `resume_decided(review_id, runner)`：approve/edit → `DurableRunner.resume(plan)`
  （waiting_review→running，已完成 step 从 checkpoint 恢复、**不重执行**，run_id 不变）；
  reject → 持久化 cancel + resume（run 图走到 cancelled，事件流完整）。
- edit 经工具数据通路生效（resume 后的 finalize 从 review 库读回 edited_answer），plan 与
  config fingerprint 不变，因此 M3 的 CheckpointMismatch 保护不被绕过。
- `ScriptedReviewer`：确定性替身（无 unsupported 且 expected_facts 齐 → approve，否则 reject），
  决策输入存入 `decision_inputs`，`replay()` 可逐位重放并校验（B3 决策可重放的实现）。

## 2. 指标口径

| 指标 | 口径 |
|---|---|
| claim support rate | verifier 判定 supported 的 claim / 总 claim |
| citation precision | 绑定证据 page == gold_anchor.page 的绑定 / 总绑定（单文档语料，doc 恒等） |
| citation coverage | 有 ≥1 绑定的 claim / 总 claim（= claim map coverage） |
| numeric accuracy | 通过全部检查的数字/单位类 claim / 数字/单位类 claim 总数 |
| unsupported rate | unsupported claim / 总 claim |
| fact recall | expected_facts 命中数 / 总数（格式容忍：逗号、`= 两侧空格`、连字符；词边界匹配） |
| escalation precision/recall | 预测 = verifier `human_review`；gold 标签为**派生标签**（见 §4 声明） |
| 延迟 / token | 每 arm 端到端 wall time；B1–B3 token 取自 DeepSeek usage，B0 HTTP 端点不暴露 token（记 None） |

## 3. B0–B3 方法学

冻结问题集：`benchmarks/answer_cases.jsonl`（12 条，由 `baseline_cases.jsonl` 改造，附
`derived_from` 回溯）。冻结证据：`artifacts/baseline/m0-20260922/evidence.db`（119 条 text，
attention 论文；**运行时复制到临时文件只读使用**，原 artifact 永不以读写方式打开）。

- **B0** 现有单 AgentExecutor：真实 HTTP `POST http://127.0.0.1:8080/query`
  （服务内 deepseek-v4-pro + 其自带 RAG）。单次尝试，超时 120s；失败则库级 DeepSeek+BM25
  兜底（重试 ≤2，超时 120s），mode 字段如实记录。
- **B1** Planner+Tools：M2 `ExecutionPlan`（kb.search→answer.draft→answer.finalize）由 M3
  `DurableRunner` 执行，检索为 M4 pipeline 的 BM25 单路（dense 端点不可用，声明于 §4），
  draft 走 DeepSeek（temperature=0，提示词强制 [pN] 引用与"无证据明说"）。
- **B2** B1+Verifier：finalize 前挂 review gate，run 暂停于 waiting_review，verifier 判定后
  记录 decision 并无条件放行（observer 模式，用于量 verifier 信号本身）；`retrieve_more`
  触发一次证据池 5→10 扩展并复检（不重新生成答案）。
- **B3** B2+Human Review Gate：`human_review` → 入队 → ScriptedReviewer 决策 →
  **原 run resume**（approve）或 cancel（reject，最终不提交答案，计为未编造）。
  决策经 `replay()` 重放校验。

所有外部调用：超时 ≤120s、重试 ≤2；任一 arm 失败记 `error` 并继续，绝不整体挂起。

## 4. 不对称性与口径声明（必读）

1. **B0 检索不可观测**：8080 服务自带 RAG 不向客户端暴露检索明细，B0 的 claim 级指标
   是在共享 BM25 池上测量的，与 B1–B3 的"自身检索证据"口径不同。B0 与 B1 的差异
   因此含有检索组件差异，不只是控制平面差异。
2. **escalation gold 标签是派生的**，非人工标注：`gold_escalate = B2 验证存在
   unsupported/conflict，或 B2 草稿 expected_facts 未全覆盖，或 refusal case 提交了
   非拒答答案`。这会高估 verifier 与 gold 的一致性（标签部分来自 verifier 自身输出），
   因此 escalation precision/recall 只作结构验证，不作效果宣称。
3. **B1–B3 各自独立调用 LLM**（temperature=0 但端点非严格确定），arm 间答案文本不完全
   相同；对比以聚合指标为准。
4. 检索为 BM25 单路：dense 向量库（local bge）在离线实验环境未初始化；M4 消融已证明
   融合收益，此处声明后采用单路，对四个 arm 一致。
5. citation precision 的分母绑定 page 来自 nougat 解析页码，gold_anchor.page 为人工标注
   PDF 页码，两者可能存在 ±1 偏移；该指标按严格相等计算，属保守下界。

## 5. B0–B3 结果（冻结集实测，2026-09-22）

见 `artifacts/verification/m7-20260922/summary.json` 与 `cases/*.json`（每 case 明细含
答案原文、逐 claim checks、review 决策与 resume 轨迹）。

| 指标 | B0 (HTTP 单 Agent) | B1 (+Planner/Tools) | B2 (+Verifier) | B3 (+Review Gate) |
|---|---|---|---|---|
| claim support rate | 0.242 | 0.350 | 0.267 | 0.800 * |
| citation precision | 0.500 | 0.500 | 0.500 | 1.000 * |
| citation coverage | 0.524 | 0.575 | 0.700 | 1.000 * |
| numeric accuracy | 0.267 | 0.225 | 0.325 | 0.667 * |
| unsupported rate | 0.476 | 0.425 | 0.300 | **0.000** * |
| fact recall | 0.808 | 0.458 | 0.408 | 0.167 |
| refusal case 正确（/2） | 2 | 2 | 2 | 2 |
| escalations (human_review) | 0 | 0 | 9 | 8 |
| escalation precision / recall | – / 0.0 | – / 0.0 | 1.00 / 0.82 | 1.00 / 0.73 |
| latency mean (ms) | 22124 | 6928 | 4549 | 4646 |
| prompt+completion tokens | n/a（HTTP 不暴露） | 6566+2354 | 6566+2712 | 6566+2798 |

\* B3 的 claim 级均值只统计**实际提交**的答案：6/12 草稿被 scripted reviewer 拒绝后
run 走 cancelled、不提交答案（这些 case 不计入 support/citation 均值，fact_recall 计 0）。
这是 gate 的设计效果而非统计粉饰——被拒答案逐案可查（cases/*.json 的 review_decision
与 final_run_state）。

延迟口径：B0 含服务端 RAG+agent 全链路（22.1s）；B1–B3 为离线管线（检索+一次生成，
4.5–6.9s），B2/B3 的 review 为本地确定性判定，增量可忽略。token 增量：B2/B3 相对 B1
仅差 completion 文本长度（verifier 与 reviewer 均为规则实现，**0 额外 LLM token**）。

### Verifier 收益结论

**有据（有条件成立）**。在冻结集（n=12）上：

1. B2 verifier 对 9/12 case 发出 human_review，escalation precision 1.0、recall 0.82
   （对派生 gold 标签，见 §4-2）；漏报 2 例（AN-R01 的拒答草稿、AN-V01/V02 中一例）。
2. B3 提交答案的 unsupported rate 从 B1 的 0.425 降到 **0.0**，citation precision
   0.5→1.0，numeric accuracy 0.225→0.667——收益来自"拦截"而非"修复"。
3. 代价：6/12 草稿被拒后直接弃答（当前无修订回路），fact recall 0.458→0.167。

结论：**保留 Verifier，不建议简化**；但 review gate 的 reject 分支应接"带 verifier
反馈的修订重生成"回路（M8 候选），把拦截收益转成修复收益，否则高 precision 是用
覆盖率换来的。样本量小（12 条、单文档语料），数字不外推。

## 6. Deferred 清单（等 VLM key）

视觉部分本里程碑只交付工具链与合成 fixture 测试，**未产生任何真实视觉数据**：

1. 真实 02_visuals_kb.jsonl 产出后，用 `visual_qa.check_records` 跑全量 QA（空描述/错页/
   错图号/低信息），输出缺陷归因分布（框架已就绪，合成样例见 `benchmarks/visual_cases.jsonl`）。
2. text-to-visual / visual-to-text 检索评估：`visual_eval.run_visual_eval(kb_path,
   results_path, gold_path)`；缺输入时返回 `status="deferred"` 与原因，**metrics=None**。
3. AN-V01/AN-V02 的真实视觉问答（当前答案来自文本证据，Figure 1/2 布局描述未经 VLM 核验）。
4. 视觉 claim（claim_type=visual）与视觉证据（modality=visual）的绑定评估——M1 库中目前
   0 条 visual 证据，claim map 对 visual claim 的阈值标定待真实数据。
5. 视觉失败归因层次（解析失败 → 描述低信息 → 检索 miss → 生成 hallucination）的实证分布。

## 7. 测试

`./runtime/bin/python3.12 -m pytest tests/verification tests/review -q`（47 项）：
claim 拆分稳定性与 LLM 回退、数字/单位/冲突绑定、verifier 升级策略、review
入队→决策→**原 run resume 真实闭环**（M3 DurableRunner，approve/reject/edit 三路径）、
visual_qa 合成样例、deferred 路径不产假数据。
