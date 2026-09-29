# S11-A 理解轨讲义：Release Gate 可比性元数据与两条红灯的根因

> 范围：S11-A（T1 诊断）。只读代码、只写文档，本讲义不改任何代码、不提交。
> 现状验证日期：2026-09-29
> 事实来源：两条红灯的根因已由两个独立诊断代理分别核实（检索漂移代理、verification 语义代理），本讲义直接采用其结论，不做重复调查。
> 读者：后续维护者与面试官。回答的问题是——**Gate 为什么亮红灯？红灯是回归、噪声，还是尺子本身的问题？**

---

## 1. 人话摘要

S10 跑完，Release Gate 亮了两条红灯：`retrieval.recall@20` 相对 M8 baseline 漂移 −0.000991，`verification.hr` 从 0.389 涨到 0.458。诊断结论一句话：**两条红灯都不是代码回归**。

- recall 红灯是**语料版本漂移**：M8 跑的时候 evidence.db 里只有 v1 摄取，S10 跑的时候读到的是 v3 摄取，同一 PDF 重新解析后 evidence_id 全变、chunk 文本略变、分数略变。检索代码全链路确定性，逐 case 可查。真正的缺口是 `app/eval/fingerprint.py` 的指纹里**没有语料内容哈希**，Gate 根本不知道两次运行用了不同版本的语料。
- hr 红灯是**指标语义混淆**：hr 衡量的是「生成器的幻觉率」而不是「verifier 的严格度」——B2 臂里 review 决策被无条件 resume 后原样提交，verifier 只是旁观者。再加上 split_claims 规则改过（8490cf2）和 draft 来自实时 DeepSeek 调用，M8 和 S10 的 hr **本来就不可比**。

处置红线：**不放宽阈值、不删 case、review 不算 pass、不覆盖 M8/S10 历史产物**。

---

## 2. 主链流程图与代码落点

```
┌─────────┐   ┌────────────┐   ┌─────────┐   ┌─────────────┐
│  query  │──▶│ retrieval  │──▶│ context │──▶│ draft claims│
└─────────┘   └────────────┘   └─────────┘   └─────────────┘
              app/retrieval/    app/context/   app/verification/
              pipeline.py       engine.py      claims.py (split_claims)
              ├ bm25.py                        experiment.py (draft 生成)
              ├ fusion.py (RRF)
              ├ rerank.py (proxy)
              └ gate.py (authority)
                    │
                    ▼
┌──────────┐   ┌───────────┐   ┌─────────────────────────┐
│ release  │◀──│   eval    │◀──│     verification        │
│  gate    │   │  metrics  │   │  accept / revise / review│
└──────────┘   └───────────┘   └─────────────────────────┘
app/eval/      app/eval/       app/verification/
release_gate.py├ adapters.py    answer_tools.py (answer_verify_handler)
               └ runner.py      experiment.py (measure_arm / B2 臂)
```

关键对照：生产链路的 `answer_verify_handler`（app/verification/answer_tools.py:249-332）与 eval 的 B2 臂（app/verification/experiment.py:408-442）是**两条不同的路**——这正是 hr 红灯的病根之一，见第 4 节。

---

## 3. 红灯一：retrieval.recall@20 漂移 −0.000991

### 3.1 先排除代码不确定性

检索管线全链路有确定性 tie-break，排序键如下：

| 环节 | 排序键 | 代码 |
|---|---|---|
| BM25 | `(-score, evidence_id)` | app/retrieval/bm25.py:84-85 |
| RRF 融合 | `(-score, best_src_rank, evidence_id)` | app/retrieval/fusion.py:100-103 |
| proxy reranker | `(-proxy, pre_rerank_rank, evidence_id)` | app/retrieval/rerank.py:102-105 |
| authority gate | `(-score, rank, evidence_id)` | app/retrieval/gate.py:136-137 |

已有回归测试锁定该行为：tests/retrieval/test_fusion.py:33、tests/retrieval/test_rerank_and_mapping.py:38。**同一语料跑两次，结果必然一致**——所以漂移只能来自输入（语料），不是来自代码。

### 3.2 根因：evidence.db 语料版本漂移

- M8 baseline 跑于 2026-09-22 10:57（git 09294df），当时 evidence.db 只有 **v1 摄取**（valid_from=2026-09-22T03:27:32）。
- S10 跑时，检索以 `current_only=True`（app/retrieval/bm25.py:20,35）读到的是 **v3 摄取**（valid_from=2026-09-23T12:22:20）。
- 同一 PDF 重新解析 → evidence_id 全部不同、chunk 文本略不同 → BM25/dense 分数略不同 → top-20 成员变动。

### 3.3 逐 case 证据

| case | M8 → S10 recall@20 | 解释 |
|---|---|---|
| RET-N01 | 0.2 → 0.1 | v1 时第 2 个 page-5 chunk 排第 15 名；v3 掉出 top-20 |
| RET-T04 | 0.2307 → 0.3077 | 反向受益（v3 语料反而召回更多 gold） |
| BL-T05 | 0.0 → 0.0 | 两次一致 fail，见 3.4 |

净漂移 −0.000991 是**真实的数据差异**：不是噪声，也不是代码回归。三个 case 有升有降，是典型「语料换了」的指纹。

### 3.4 BL-T05：一个稳定的真实召回缺陷（保留 fail）

BL-T05（benchmarks/baseline_cases.jsonl，问题 "Which two English machine translation tasks are used for evaluation?"，gold anchor 在 page 6）：

- evidence.db 里有 **13 条** current、非 degraded、page=6 的 text 证据（document_id=e8a365c1d8815226）——gold 在库里、可达；
- 但 BM25 和 dense **两路都没有**把任何 page-6 chunk 召回进融合池（trace 中 anchor_protection=[]）；
- 两次运行一致 fail，**不贡献漂移**。

这是真实的召回缺陷记录。S11-B 的处置：**保留 fail 并记录证据，不删 case、不改 skip**。

### 3.5 真正的缺口：指纹里没有语料内容哈希

`app/eval/fingerprint.py` 的 config_fingerprint（fingerprint.py:131-147）只记录 benchmark 文件哈希 + git commit + dirty + 依赖版本，**不记录 evidence DB / vector store 的内容哈希**。后果：两次运行用了不同语料版本，指纹却看起来"同配置"，Gate 只能按「同一基准下的退化」误报红灯。这就是 Gate **可比性元数据缺口**。

---

## 4. 红灯二：verification.hr 0.389 → 0.458

### 4.1 hr 的定义

hr（hallucination rate）= answer_cases 上「每 case unsupported_claims / total_claims 的均值」。claim 来自规则版 `split_claims`，unsupported 来自 claim-evidence map。

- 指标定义：app/eval/adapters.py:1062-1066（是 unsupported_rate 的别名）；
- per-case 计算：app/verification/experiment.py:324-345（measure_arm）。

### 4.2 语义混淆：review 被混入了「生成器幻觉」

eval 的 B2 臂**无条件 resume** 并把 draft 原样 commit（app/verification/experiment.py:408-442，docstring 自述 "decision recorded, run always resumes"）。也就是说：

- verifier 只是**观察者**，它的 accept/revise/review 决策不影响最终提交的答案；
- S10 那次 12 个 case 里 **8 个 human_review 全部计入 hr**；
- 所以 hr 衡量的是「**生成器幻觉率**」，不是「**verifier 严格度**」。

对照生产链路的 S6 `answer_verify_handler`（app/verification/answer_tools.py:249-332）：那里有真正的「committed vs draft」区分——`final_answer = draft if action == "accept" else None`（answer_tools.py:265），review/abstain 时 `output["answer"] = None`。**eval 的 B2 臂没有走这条路**，把「需要人审」当成了「已发布的幻觉」。

### 4.3 M8 vs S10 本来就不可比

两个独立原因叠加：

1. **evaluator 版本变化**：commit 8490cf2 改了 app/verification/claims.py 的 `split_claims`（citation marker 先遮蔽再切句、实体正则收紧）。同一答案文本，新旧规则会切出不同的 claim 数，hr 的分母分子一起变。
2. **LLM 采样噪声**：answer_cases 的 draft 是**实时 DeepSeek 调用**（app/verification/experiment.py:104-127，temperature 0 但走远程 API，不保证位级确定）。实测同一份代码跑两次，hr 相差 0.07（0.458 vs 0.528）——M8 到 S10 的 +0.069 完全落在这个噪声带内。

### 4.4 逐 case 变化

| case | M8 → S10 hr | 备注 |
|---|---|---|
| AN-N01 | 1.0 → 0.5 | |
| AN-T01 | 0.5 → 1.0 | 拒答句本身被计为 1 条 unsupported claim |
| AN-T02 | 0.5 → 1.0 | |
| AN-T03 | 0.5 → 0.3333 | |
| AN-T04 | 0.5 → 1.0 | |

注意 AN-T01：split_claims 把「我不知道/无法回答」这类拒答句也切成了 claim，而拒答句天然没有 evidence 支撑——这是规则版切分的已知边界，不是幻觉恶化。

### 4.5 版本元数据现状

- eval 报告只有 `schema_version=radiant-eval-report/v1`（app/eval/runner.py:57），**没有** metric_schema_version / evaluator_version；
- config_fingerprint（fingerprint.py:131-147）有 git commit + dirty + 依赖版本 + 数据文件哈希，但 `dirty=true` 削弱了 git pin 的效力，且没有 evidence DB 内容哈希。

---

## 5. 结论与 S11-A/B/C 处置方向

### 5.1 recall 红灯 = 数据版本漂移（Gate 缺可比性元数据）

- **S11-A**：给指纹补上 evidence DB / vector store 内容哈希；Gate 增加兼容性检查——两次运行语料指纹不一致时，输出 `baseline_incompatible`，而不是把数据漂移报成退化红灯。
- **S11-B**：补重复性测试（同语料跑两次结果一致）与全阶段 trace，让逐 case 归因不用手工挖。
- **BL-T05**：保留 fail，作为真实召回缺陷的证据记录，不删不改 skip。

### 5.2 hr 红灯 = 指标语义混淆 + evaluator 版本变化 + LLM 采样

- **S11-C**：把 hr 拆成两个语义清楚的指标——
  - `detected_unsupported_rate`：draft 级、verifier 观察者视角（现在的 hr 实际语义）；
  - `final_committed_unsupported_rate`：仅对 accept 后真正提交的答案计算（对齐 answer_tools.py 的 committed vs draft 语义）。
  Gate 改用后者；旧指标保留但在报告中标注「与历史不可比」。

### 5.3 红线（任何阶段不得越过）

1. 不放宽 Gate 阈值来让红灯变绿；
2. 不删 case、不把 fail 改成 skip；
3. review 决策不算 pass（人审之前答案不算发布）；
4. 不覆盖 M8 / S10 历史产物——它们是诊断的事实来源。

---

## 6. 一句话记住本讲义

> recall 红灯是「两次考试用了不同的题库，但成绩单的表头没写题库版本」；hr 红灯是「把'待人审'算成了'已发布的幻觉'，而且阅卷规则中途还换过」。修的不是模型，是**尺子和尺子的出生证明**。
