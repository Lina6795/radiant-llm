# S11-C 理解轨讲义：Verification 指标语义——detected 与 committed 为什么要分开

> 范围：S11-C（T1 讲义）。本讲义只解释，不改任何代码、不提交。
> 现状验证日期：2026-09-29
> 事实来源：指标定义与代码落点已由诊断代理逐一核实，本讲义直接采用其结论；文中 `path:line` 均可打开核对。
> 读者：后续维护者与面试官。回答的问题是——**verification 各指标到底在量什么？为什么「验证器更严格」不等于「答案更差」？S11-C 如何把两套语义拆开？**

---

## 1. 人话摘要

S11-A 已经诊断出 hr 红灯的病根：**一个指标混了两套语义**——verifier 在 draft 上「检测到多少风险」和「用户实际收到的答案有多少风险」被搅在一起。S11-C 的修法不是改模型，而是**把尺子拆开**：

- **detected 族**（新）：verifier 作为观察者，在 draft 阶段看到的 unsupported 比例。它衡量的是「草稿风险 / verifier 严格度」。
- **committed 族**（新）：只对**真正提交给用户**的答案统计的 unsupported 比例。它衡量的才是「最终交付质量」。
- **旧 hr / unsupported_rate**（保留不动）：历史口径——B2 臂无条件 resume、review 答案混入统计。保留作诊断，但 S11-C 起 **Gate 不再观察它**。

一句话：detected 是「安检仪报警率」，committed 是「放上飞机的行李里危险品占比」。安检仪调严了，报警率上升，但放上去的危险品只会变少或不变——**两件事不能用一个数讲**。

---

## 2. 指标族谱：准确定义与代码落点

### 2.1 S11-C 新增指标（detected / committed / outcome 三族）

| 指标 | 定义 | 代码落点 |
|---|---|---|
| `detected_unsupported_rate` | verifier 在 **draft claims** 上检测到的 unsupported 比例（gate-time 决策值，verifier 是观察者） | per-case：app/verification/experiment.py:447-449（`round(len(decision.unsupported) / len(claims), 4)`，在 re-verify 之前、用 gate-time 的 decision）；聚合：app/eval/adapters.py:997-998 |
| `final_committed_unsupported_rate` | 只对**真正提交**的答案（`verification_action` ∈ {commit, retrieve_more}）统计的 unsupported 比例；human_review / clarify / abstain 不提交任何答案，整行排除 | committed 行筛选：app/verification/experiment.py:450-451（`committed` 布尔）；聚合：app/eval/adapters.py:999-1002 |
| `review_rate` | 升级人工审核的比例：`verification_action == "human_review"` 的行数 / 有决策的行数 | app/eval/adapters.py:1005-1009 |
| `accept_rate` | 自动接受比例：`verification_action == "commit"` 的行数 / 有决策的行数 | app/eval/adapters.py:1010-1013 |
| `supported_claim_rate` | claims 中 supported 的比例。**值与 `claim_support_rate` 完全相同**，作为 Gate 稳定命名保留的别名 | app/eval/adapters.py:1003 |

per-case 的 `committed` 布尔是整个分离的关键。experiment.py:450-451 的注释写得很直白：commit / retrieve_more 会把答案交付给用户；human_review / clarify / abstain **commit nothing，绝不能算作被接受的答案**（experiment.py:441-446 注释块）。

### 2.2 旧指标：保留不改，但语义要说清

| 指标 | 定义 | 语义陷阱 |
|---|---|---|
| `hr` / `unsupported_rate` | B2 臂**无条件 resume** 把 draft 原样提交后，用 widen 后的 evidence **re-verify** 得到的 unsupported 比例 | **review 答案被混入**——verifier 判了 human_review 的答案照样被提交、照样计入分母分子。这是历史口径，衡量的是「生成器幻觉率」而非交付安全 |

落点：per-case 计算在 measure_arm（app/verification/experiment.py:324-345，:340 的 `unsupported_rate`）；B2 臂无条件 resume 在 run_b2（experiment.py:408-453，docstring 自述 "decision recorded, run always resumes"）；`hr` 是 `unsupported_rate` 的逐值别名（app/eval/adapters.py:990-993）。

### 2.3 其他保留指标（语义未变，继续有效）

聚合全部在 `_aggregate_answer_metrics`（app/eval/adapters.py:963-1041）：

- `claim_support_rate`（:985）：claims 的 supported 比例（committed 与否都计入，沿用历史口径）；
- `citation_precision` / `citation_coverage`（:986-987）：引用命中 gold 页 / claim-证据覆盖率；
- `numeric_accuracy`（:988）：数字 claim 与证据数值一致率；
- `fact_recall`（:994）：expected_facts 在答案中出现的比例；
- `escalation_precision` / `escalation_recall`（:1015-1032）：升级决策相对 gold 标注的查准/查全；
- `refusal_cases_correct`（:1033-1040）：refusal 类 case 中正确拒答（refused 或 answer 为空）的计数。

---

## 3. 为什么「验证器更严格」不等于「答案更差」

### 3.1 因果链

```
verifier 阈值/规则变严
   │
   ▼
detected_unsupported_rate ↑        ← 草稿被标出的问题变多（观察者视角）
   │
   ▼
review_rate ↑ 、accept_rate ↓      ← 更多答案被拦进 human_review / abstain
   │
   ▼
final_committed_unsupported_rate →或↓  ← 提交出去的答案反而更少带 unsupported
```

**最终答案安全性只看 committed 族**：只有 accept/commit 路径交付给用户的答案才计入 `final_committed_unsupported_rate`。被拦下的答案不交付，自然不构成用户侧风险。detected 端上升恰恰说明拦截在起作用，而不是安全性下降。

### 3.2 旧 hr 为什么跨版本不可比

1. **evaluator 版本变化**：commit 8490cf2 改了 app/verification/claims.py 的 `split_claims`（citation marker 先遮蔽再切句、实体正则收紧）。同一段答案文本，新旧规则切出的 claim 数量和类型不同 → hr 的分母分子一起变，两个版本的 hr 没有共同标尺。
2. **LLM 采样噪声**：answer_cases 的 draft 是**实时 DeepSeek 调用**（experiment.py:104-127，temperature 0 但走远程 API，不保证位级确定）。实测同一份代码跑两次，hr 相差 0.07（0.458 vs 0.528）。
3. **口径混淆**：如上节，review 答案混入 hr，S10 那次 12 个 case 里 8 个 human_review 全部计入。

### 3.3 历史结论

M8（hr 0.389）vs S10（hr 0.458）的红灯因此被判定为**指标语义 + evaluator 版本 + 采样**三重不可比，不是质量回退。M8→S10 的 +0.069 完全落在同代码重复运行的噪声带（±0.07）内。处置不是放宽阈值，而是换一把语义清楚的尺子——这正是 S11-C 做的。

---

## 4. B2 臂数据流：两套语义在流水线上的位置

```
                    ┌────────────────────────── B2 臂（eval）──────────────────────────┐
                    │                                                                  │
 draft (LLM) ──▶ verify_answer ──▶ gate-time decision                                  │
                    │              (commit / retrieve_more /                           │
                    │               clarify / human_review / abstain)                  │
                    │                  │                                               │
                    │                  ├─▶ detected_unsupported_rate  ←── 观察者指标     │
                    │                  ├─▶ committed 布尔              ←── 交付判定      │
                    │                  │                                               │
                    │   retrieve_more? ├─ yes: 扩 evidence top-10 再 verify (2 rounds) │
                    │                  │                                               │
                    ▼                  ▼                                               │
              无条件 resume ──▶ finalize（原样提交 draft）                              │
                    │                                                                  │
                    ▼                                                                  │
         measure_arm(final, widened evidence)                                          │
                    │                                                                  │
                    ├─▶ unsupported_rate / hr  ←── 旧口径：re-verify，review 混入        │
                    └─▶ final_committed_unsupported_rate  ←── 仅 committed=True 的行    │
                    └──────────────────────────────────────────────────────────────────┘
```

落点对照：run_b2 全函数在 app/verification/experiment.py:408-453；gate-time verify 在 :417，retrieve_more 扩证据在 :420-424，无条件 resume 在 :427-430，新字段 `gate_claims` / `detected_unsupported_rate` / `committed` 在 :447-451，re-verify 测量在 :452。

### 4.1 与 S6 生产链的对照

生产链路的 `answer_verify_handler`（app/verification/answer_tools.py:249-332）**一直**有真正的 committed 区分：

- `final_answer = draft if action == "accept" else None`（answer_tools.py:265）；
- revise 失败降级 review 时 `final_answer = None`（:304-305）；review/abstain 时 `output["answer"] = None`——不交付任何东西。

也就是说：**生产链没有语义混淆，混淆只存在于 eval 的 B2 臂**（旧口径无条件 resume）。S11-C 不改成 B2 的 resume 行为（那是历史实验设计），而是用 `committed` 布尔 + 两套指标在**测量层**补上区分——eval 记录的事实不变，解读方式对齐了生产语义。

---

## 5. Gate 规则变化

### 5.1 Gate 改看 committed 族

`GateConfig.unsupported_metrics` 的默认值从 `verification.hr` 改为 `verification.final_committed_unsupported_rate`（app/eval/release_gate.py:60-62）。规则仍是「不得上升」且 **fail-closed**：baseline 测了而 current 没测 → 直接 fail，拒绝在更少测量下放行（release_gate.py:226-227）。

### 5.2 旧指标的去留

- `hr` / `unsupported_rate` **保留在报告里**（adapters.py:989-993），不删除历史字段；
- 但跨 verifier 版本标记为**不可直接比较**——文档与注释层面明示（release_gate.py:11-14 的模块 docstring：legacy hr "is NOT gate-comparable across verifier versions"）；
- **M8 baseline 不被覆盖**：M8/S10 历史产物是诊断的事实来源，处置红线不动。

### 5.3 与 S11-A 的 baseline_incompatible 配合

指标换尺子解决「同一版本内的语义」，S11-A 的兼容性检查解决「不同版本之间能不能比」：

- 指纹带 `evaluator_version`、`layer_evaluator_versions`、`metric_schema_version`（app/eval/fingerprint.py:263-265）；
- verification 域还带 **claims/verifier 源码摘要**（fingerprint.py:237-245：split_claims 或判定规则一变，digest 就变）；
- 两次运行这些字段不一致 → Gate 输出 `baseline_incompatible`，**直接拒绝比较**、抑制所有回归规则（release_gate.py:125-171、:336、:358），而不是把「尺子换了」报成「质量退化」。

8490cf2 这类 claims.py 改动今后不会再造成 M8 vs S10 式的假红灯。

---

## 6. S11-C 测试清单（已存在，供查阅）

### 6.1 tests/eval/test_verification_metrics.py（聚合语义，纯内存构造行）

- `test_committed_metric_excludes_review_and_abstain`（:53）：accept/revise/review 等六类行（含空 claims、全 unsupported、部分 supported）下，committed 指标只统计交付行；
- `test_detected_uses_gate_time_values`（:60）：detected 取 gate-time 值，与 re-verify 值区分；
- `test_outcome_rates_over_decided_cases`（:69）：review_rate / accept_rate 的分母是「有决策」的行；
- `test_supported_claim_rate_alias_measured`（:77）：别名与 claim_support_rate 同值；
- `test_no_committed_rows_is_not_measured`（:84）：无交付行 → `not_measured`，不伪造 0；
- Gate 行为（:104-123）：hr 涨 0.3→0.9 **不 fail**；`final_committed_unsupported_rate` 涨则 **fail**；current 缺测该指标则 **fail-closed**。

### 6.2 tests/eval/test_verification_adapter.py（B2 真跑，stub LLM）

- `test_b2_arm_real_execution`（:90）：M7 机器真实跑通（plan → gated runner → WAITING_REVIEW → resume）；
- `test_b2_metrics_measured`（:98）：11 个指标全部 measured；
- `test_b2_cases_traceable_to_run_and_verdict`（:119）：每 case `committed` 是 bool 且与 `verification_action` 一致，human_review 必为 `committed=False`；
- `test_refusal_cases_measured`（:134）与 `test_skips_without_api_key`（:61）：refusal 计量与无 key 时 skip（不伪造）。

---

## 7. 一句话记住本讲义

> detected 是安检仪报警率，committed 是放上飞机的行李里危险品占比；hr 是把两者搅在一台换过零件、还会手抖的旧秤上。S11-C 没有让答案变好或变差——它让「好」和「差」第一次有了各自独立的数。
