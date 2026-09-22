# M8 — Eval Harness、Observability 与数据飞轮底座

日期：2026-09-22。范围：`app/eval/`、`app/observability/`、`tests/eval/`、`artifacts/eval/`。只读复用 M0–M6 的冻结用例集与各层库，不改任何已有模块。

## 单命令用法

```bash
cd /mnt/lina/radiant-llm
# 全部分层评测（机器可读 report.json + 人类可读 report.md）
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --all --out artifacts/eval/<run_id>/

# 单跑某层（可重复 --layer）
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --layer retrieval --out artifacts/eval/<run_id>/

# 与基线对比（report 内嵌 metrics 差异段）
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --all \
  --out artifacts/eval/<run_id>/ --baseline artifacts/eval/m8-baseline-20260922/report.json

# Release Gate（exit 0=pass / 1=fail，逐规则明细）
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.release_gate \
  --current artifacts/eval/<run_id>/report.json \
  --baseline artifacts/eval/m8-baseline-20260922/report.json

# 注册表发现（哪些数据集在/缺/未注册）
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --list
```

测试：`PYTHONPATH=app ./runtime/bin/python3.12 -m pytest tests/eval -q`。

## 用例集注册表（app/eval/registry.py）

每个数据集声明：路径、属主层、执行器 adapter、指标族、基线结果文件位置。发现机制 = 扫描 `benchmarks/*.jsonl` + 注册表匹配：

| 数据集 | 属主层 | 执行器 | 指标族 | 基线结果 |
|---|---|---|---|---|
| router_cases (12) | control | `control.router` 直接调 `RuleRouter` | deterministic | — |
| policy_cases (16) | control | `control.policy` stub router/planner 过 Guard+Policy | deterministic | — |
| runtime_cases (8) | durable | `durable.scenarios` 复用 `tests/durable/scenarios.py` 故障注入驱动 | deterministic | — |
| retrieval_cases (16) | retrieval | `retrieval.pipeline` A4 默认配置跑全量 case | deterministic+operational | `artifacts/retrieval/m4-20260922/metrics.json` |
| baseline_cases (12) | retrieval | 同上（M0 冻结问题集） | deterministic+operational | 同上 |
| context_cases (12) | context | `context.cases` 12 个按 case_id 分派的离线驱动 | deterministic | `artifacts/context/m5-20260922/metrics.json` |
| memory_cases (17) | memory | `memory.gates` Write/Read Gate/supersede/delete 驱动 | deterministic | — |
| answer_cases (12) | verification | `verification.answer` M7 B2 臂（planner+tools+verifier，见下） | deterministic+judge+human_audit | `artifacts/verification/m7-20260922/summary.json` |
| visual_cases (11) | verification | `verification.visual` 合成 fixture 过 `visual_qa`/`visual_eval` 真实校验 | deterministic+judge | 同上 |

缺失用例集/包一律 `skipped` + 原因记录，绝不报错、绝不默满分；`benchmarks/` 下未注册文件在 report 的 `discovery.unregistered_files` 中显式告警。

## Verification 层接线（M7）

- **answer_cases 走 B2 臂**：每 case 跑真实 M7 链路——M2 ExecutionPlan → M3 DurableRunner（answer.finalize 处 gated）→ 冻结 M0 证据库 BM25 检索 → 答案草稿 → 规则版 claim 验证（`split_claims`/`map_claims`/`Verifier`，deterministic）→ 必要时一次性证据池扩展 → resume 原 run。唯一外部调用是 DeepSeek 草稿（**被测系统的答案生成器，不是 judge**；评分全是确定性规则）。无 `OPENAI_API_KEY` 或冻结证据库缺失时整个数据集 skip 并记录原因，不伪造答案。
- **B0 臂不做进 harness**：B0 经运行中 HTTP 服务的 `/query`，其检索在服务内部、不可观测（M7 实验已声明的不对称），离线不可复现。
- **visual_cases 走合成 fixture 检查**：`qa_fixture` 经 `visual_qa.check_record`（batch 语义，共享 seen-set 保证重复检测与生产一致），期望缺陷类全部检出才 pass；`eval_fixture` 经 `visual_eval.recall_at_k`/`region_recall_at_k`，必须精确复现冻结期望值；实测召回值进 ViR 族指标。
- 指标：`claim_support_rate` / `citation_precision` / `citation_coverage` / `numeric_accuracy` / `unsupported_rate`（别名 **hr**，gate 监控项）/ `fact_recall` / `escalation_precision` / `escalation_recall` / `refusal_cases_correct` / `vir`。这些指标是确定性规则对 **LLM 生成答案** 的评分——指标本身非 judge-based，但结果随答案模型变化；每 case 记录 `answer_model` 与 verification action，可回到 run 与 verdict。
- CoP 语义档、回答质量等真正 judge-based 的指标仍 `not_measured`（无注入 judge，禁止默认满分）。

## 指标分层

| 层 | 内容 | 例子 |
|---|---|---|
| deterministic | 纯数据可算，无需模型 | exact match、Recall@K、MRR、nDCG、anchor hit、幂等/恢复/隔离、write precision、pass rate |
| judge-based | 需注入 judge callable（模型或人） | CoP 语义档、claim 支持性（HR）、回答质量 |
| human audit | 人工评分入口 + judge 抽样复核 | `manual_scores()`、judge 指标的 `human_review.agreement` |
| operational | 运行开销 | latency p50/p95、cost（可测时） |

### 论文五指标（app/eval/metrics.py）

- **CoP**：语义离散档 {0, 0.25, 0.5, 0.75, 1} + 数值保真项 `1 - |v-v*|/max(|v*|, ε)`；有数值时 `CoP = α·semantic + (1-α)·fidelity`，α 可配（默认 0.6）。
- **CiP**：有效引用 / 总引用。
- **CiH**：锚点命中二元（逐 case 0/1，按 case 求均值）。
- **HR**：unsupported claims / 总 claims。
- **ViR**：视觉事实召回 = 召回的 gold 视觉事实 / gold 视觉事实总数。

分母为 0 时返回 `None`（not_measured），不产生"虚假的 1.0"。

### Judge 口径红线

1. judge 一律是**注入的 callable**（`JudgeFn`），harness 不自带默认模型、不私自调 LLM；
2. 每次 judge 输出必须记录：**judge 模型 id、prompt 模板 sha256、重复次数**（原始逐次分数全保留），人工评分走 `manual_scores()` 记录 rater 与 rubric hash；
3. 无 judge 时 judge 类指标显式 `not_measured`（value=null），**禁止默认满分**；
4. **judge 指标不得标为完全客观**：Release Gate 要求任何已测 judge 指标附 `human_review.agreement`（人审一致性）字段，否则判 fail。

## 可追溯性

report.json（schema `radiant-eval-report/v1`）中：

- 每个 case 记录 `case_id`、`config_fingerprint`（run 级 git/env/模型/数据版本/依赖指纹）与 `artifact_uri`/`trace_uri`（逐 case 产物路径）；
- 每层 `datasets_metrics` 保留数据集级指标，`metrics_flat` 提供 `layer.metric` 扁平键供 gate 对比；
- 配置指纹含 git commit+dirty、Python、白名单 env、`configs/baseline.yaml` 模型段及其 sha256、每个 `benchmarks/*.jsonl` 的 sha256+case 数、关键依赖版本。

## Release Gate 规则（app/eval/release_gate.py）

| 规则 | 语义 | 缺省阈值 |
|---|---|---|
| `core_recall` | 核心 Recall（retrieval.recall@20 / anchor_hit@20 / recall@5）不退化；基线有而本次无 → fail-closed | `recall_max_drop=0.0`（可配） |
| `unsupported_rate` | HR 不上升；基线已测而本次未测 → fail-closed；两侧均未测 → skip | `unsupported_max_increase=0.0`（可配） |
| `integrity` | durable（恢复/幂等）与 memory（workspace 隔离）全 case 通过，context 的 CTX-T05/T07/T08（abstain/隔离/压缩红线）必过；完整性层被 skip → fail | 固定 |
| `judge_human_audit` | 已测 judge 指标必须附 `human_review.agreement` | 固定 |
| `layer_status` | 任何层不得相对基线 newly failed | 固定 |

输出 `pass/fail` + 逐规则明细（baseline 值、current 值、阈值、原因）。阈值可用 `--config gate.json` 覆盖（`GateConfig` 字段，未知键报错）。

## Observability（app/observability/）

- `trace.py`：统一 trace schema（`radiant-trace/v1`：run_id, layer, stage, case_id, inputs/outputs 摘要, latency, cost, config_fingerprint, artifact_uri, status）。三个适配器把既有层输出映射进来：**M4 retrieval trace**（逐 stage + final）、**M3 EventStore 事件流**、**M5 ContextDecision**（含压缩 lineage 哈希），均以真实数据验证（`tests/eval/test_observability.py` 用 `artifacts/retrieval/m4-20260922` 真实 trace、真实 EventStore、真实 ContextEngine 决策）。本地 EventStore/结果文件是事实源；LangSmith/Langfuse 仅可选分析层。
- `bad_cases.py`：Bad-case Registry（JSON 存储）：id / 发现日期 / 来源 / 现象 / 归因层 / root_cause / 修复 commit / regression case / 状态（open→fixing→fixed→verified）。`export_ledger()` 输出台账 Markdown 表，供人工粘贴进 `RADIANT-Control_执行跟踪与数据飞轮.md`。

## 数据飞轮

```text
Trace / Review / User Feedback
→ 脱敏与来源检查
→ 失败归因（bad_cases.py 登记，归因到层）
→ 固定为 regression case（benchmarks/*.jsonl 冻结）
→ 单变量修改
→ 离线回归（eval.runner --all）
→ Release Gate（eval.release_gate vs 基线）
→ 发布
```

第一版飞轮只更新 Prompt、Policy、Tool Schema、检索、Context 和测试集，不自动训练模型。

## 已知边界

- answer_cases 的 B2 臂需要 `OPENAI_API_KEY`（DeepSeek 草稿端点）；无 key 时该数据集 skip（visual_cases 不受影响，始终离线可跑）；
- retrieval 层需要 M0 evidence DB + Chroma 向量库（`RADIANT_EVIDENCE_DB` / `RADIANT_VECTOR_STORE`，缺省回退 `artifacts/baseline/m0-20260922/`），缺失则整层 skip；
- CTX-T11 的 prose 相对误差阈值取自冻结用例（<0.15），语料若变化该 case 可能转为 fail —— 这是预期行为（红线条款宁可 fail 不可放水）。
