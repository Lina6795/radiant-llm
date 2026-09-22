# M5 — Context Budget 与 Anchor Preservation

状态：已完成（2026-09-22）。本 milestone 交付独立的 `app/context/` 库与压力实验，
**未修改 `radiant_llm.py`**——上游 `chars//4` 粗算 token、70%/85% 硬编码阈值、
overflow 不保证证据保留的缺陷仍在，主控在后续 milestone 统一接线。

## 1. Token 计数（`app/context/tokenizer.py`）

全库统一经 `TokenCounter` 计数：

- 首选 tiktoken `o200k_base`（GPT-4o 系近似）。
- 回退启发式 `tokens ≈ 0.8·words + 0.07·chars`，系数按 o200k 在四类参考文本上
  手工标定，**误差来源明确标注**：英文技术散文 +14%、口语对话 −9%、
  LaTeX 重的 M0 语料 −28%（数学标记 token 密度异常高）、密集代码 −52%。
  根本误差来源是不同文本类别的 token 密度差异（3.9–6.2 chars/token），
  任何单一线性估计无法同时闭合。回退激活时 `is_approximate=True`，
  绝对预算应视为近似值；生产环境装 tiktoken。

## 2. 分项预算模型（`app/context/budgets.py`）

默认对齐 128k 窗口（`BudgetConfig`，全部可配）：

| 分区 | 默认配额 | 内容 |
|---|---|---|
| system | 8,192 | 系统提示、技能清单、工具 schema |
| active_turn | 16,384 | 当前用户输入 |
| memory | 24,576 | 会话记忆/摘要 |
| evidence | 49,152 | 检索证据（M4 候选） |
| artifact | 12,288 | artifact 指针/小型内联产物 |
| tool_result | 12,288 | 工具输出（隔离后 stub） |
| response_reserve | 8,192 | 模型回复预留（不属于上下文内容） |

`BudgetConfig.validate()` 拒绝配额总和超过可用窗口的配置；`BudgetUsage`
提供逐分区余量（`margin`）、超配判定（`is_over` / `over_partitions`）与
总量超配判定（`is_over_total`，以扣除回复预留后的可用量为准）。
非默认窗口用 `BudgetConfig.scale(total)` 按比例派生。

## 3. 证据选择（`app/context/selector.py`）

排序键为复合分：`0.6·relevance + 0.2·authority + 0.2·freshness`
（relevance 在候选集内归一化；authority 映射 primary 1.0 / secondary 0.7 /
tertiary 0.4；freshness 以 ~2 年时间常数指数衰减，未知取 0.5）。

- **多样性**：每页最多 `max_per_page`（默认 2）条，防止同页近重复 chunk
  占满窗口。
- **Pin（锚点保护）**：`pinned=True` 的证据**永远不被挤掉**——先于一切
  入场并优先占用预算。若 pin 证据单独就超过 evidence 配额，选择器仍然
  全部保留并置 `overflow=True`，由 engine 映射到 `abstain` 分支，
  绝不静默丢锚点。
- 每条未入选证据都带 reason code（`budget_exhausted` /
  `diversity_page_cap` / `below_min_score`）。

## 4. 压缩语义红线（`app/context/compressor.py`）

**Evidence 与 citation 内容不允许被压缩改写语义**：只能整条保留或整条
舍弃（带 reason）。`Compressor.compress_partition("evidence", ...)` 直接抛
`EvidenceCompressionError`。压缩只作用于 `memory` / `artifact` /
`tool_result` 摘要区（`COMPRESSIBLE_PARTITIONS`）。

两级压缩：

1. **规则级**（确定性，无模型）：去模板行（页脚/分隔线）、折叠冗余空白、
   长段保留首尾句 + 高价值中间句（数字/专名密度）。若首尾句骨架已超目标，
   规则级声明"不足"，不硬切。
2. **LLM 递归级**（可选，callable 注入，测试用 stub）：仅当规则级不足时
   触发；分段摘要后合并，超目标则递归，深度上限 3。token 级硬截断只是
   无 summarizer 或递归耗尽时的最后手段，且必然记录在 lineage steps 中。

每次压缩产出 `CompressionLineage`：input/output sha256、方法
（`rules` / `rules+llm_recursive`）、模型名、前后 token 数、目标、步骤列表
——**任何压缩后的片段都可追溯到压缩前内容哈希**。

## 5. 大输出隔离（`app/context/isolate.py`）

单条超过 `isolate_threshold_tokens`（engine 选项，默认 2048）的
tool_result / artifact 落盘到 `RADIANT_ARTIFACT_DIR`（或显式 root），
内容寻址（sha256 文件名，相同字节去重）。上下文只保留
`ArtifactPointer`：uri + sha256 + 摘要（前 160 字符）+ 原始大小/token 数。
`resolve(pointer, verify=True)` 读回并校验哈希，篡改会报错。

## 6. ContextEngine 决策分支（`app/context/engine.py`）

按优先级解析（高 → 低）：

| 分支 | 触发条件 | 行为 |
|---|---|---|
| `abstain` | pin 证据单独超过 evidence 配额 | 拒绝服务；锚点仍在装配结果中，绝不丢 pin |
| `clarify` | active_turn 超配额 | 用户输入替换为显式占位符 + drop 记录，请用户收窄请求；**不静默截断** |
| `retrieve_more` | 未提供任何证据 | 装配继续，但决策告知控制层回检索而非凭记忆作答 |
| `compress` | 任一分区或总量超配，经压缩+预算化选择后装入 | 全部压缩 lineage 入决策记录 |
| `assemble` | 一切在配额内 | 正常装配 |

**禁止静默截断**：每个被舍弃/替换的条目都进入 `decision.drops`
（partition、item_id、reason code、token 数）；压缩全部带 lineage；
隔离全部带 pointer。实验中 10 个 cell × 3 reps 的
`drops_without_reason` 合计为 0。

## 7. 压力实验（`artifacts/context/m5-20260922/`）

复现：

```bash
cd /mnt/lina/radiant-llm && \
  PYTHONPATH=app \
  RADIANT_EVIDENCE_DB=artifacts/baseline/m0-20260922/evidence.db \
  ./runtime/bin/python3.12 -m context.experiment \
    --output-dir artifacts/context/m5-20260922
```

### Pseudo-sources 方法学（引用数字前必读）

真实冻结语料只有 **1 篇文档**（attention 论文 119 个 text chunk）。扩展到
10/25/50/100 个真实 PDF 源在本环境不可行：Nougat CPU 解析 ~10 min/页，
100 篇约 17 CPU 小时。因此用固化 seed（20260922）的程序生成源模拟：

- **近义干扰**：真实 chunk 经固定替换表 + 句子重排改写（与 gold 竞争
  相关性与预算）；
- **无关填充**：模板生成、与查询主题不相交的句子（只消耗预算）。

检索分数同为模拟（gold ~ U(0.65,0.95)，近义 ~ U(0.45,0.90)，填充 ~
U(0.02,0.35)，seeded）：被测组件是预算/选择/压缩/装配，**不是** M4 检索栈。
候选列表之后的一切——token 计数、选择、pin 保护、压缩、隔离、决策——
都是真实 `app/context` 代码跑在真实 chunk 文本上。

**局限**：干扰源的分数分布与 chunk 长度是人为设定的，非实测；真实 100-PDF
语料会有更重的尾部、跨文档近重复与 OCR 退化 chunk。此处报告的失败 onset
是语料难度的**下界**。另外 answer 代理指标（expected facts 子串匹配）会被
保留了 "encoder"/"decoder" 字样的近义干扰源污染——100-source pin-off rep2
中 gold=0 但 facts=True 即属此类，代理指标高估了答案质量。

### 结果（mean±pstdev，3 reps；tiktoken o200k；16k 实验窗口，evidence 配额 8192）

| sources | pin | token 节省率 | gold 保留率 | facts 代理 | 装配延迟 ms | 决策 |
|---|---|---|---|---|---|---|
| 1 | on | 0.807±0.008 | 1.000±0.000 | 1.000±0.000 | 30.1±2.1 | compress |
| 1 | off | 0.821±0.010 | 0.400±0.000 | 1.000±0.000 | 26.1±1.3 | compress |
| 10 | on | 0.835±0.008 | 1.000±0.000 | 1.000±0.000 | 31.7±0.3 | compress |
| 10 | off | 0.845±0.010 | 0.400±0.000 | 1.000±0.000 | 32.0±1.2 | compress |
| 25 | on | 0.869±0.008 | 1.000±0.000 | 1.000±0.000 | 39.7±1.2 | compress |
| 25 | off | 0.891±0.004 | 0.400±0.000 | 0.833±0.236 | 39.7±1.5 | compress |
| 50 | on | 0.912±0.001 | 1.000±0.000 | 1.000±0.000 | 53.2±0.2 | compress |
| 50 | off | 0.925±0.005 | 0.400±0.000 | 1.000±0.000 | 54.5±1.7 | compress |
| 100 | on | 0.939±0.003 | 1.000±0.000 | 1.000±0.000 | 79.7±2.0 | compress |
| 100 | off | 0.942±0.003 | 0.267±0.189 | 1.000±0.000 | 79.1±2.4 | compress |

精确数值见 `metrics.json`（每个 run 完整保留，非只挑最好一次）。

### 失败 onset 观察

- **Pin 开**：1→100 sources 全程 gold 保留率 1.000、facts 1.000，无
  abstain，无残余 overflow。锚点保护在 100-source 输入 60,039 tokens
  （可用窗口 14,336，~4.2× 总量超配；evidence 候选 ~58k vs 8,192 配额
  ~7×）下仍然成立。
- **Pin 关**：gold 保留率被多样性上限钉在 2/5=0.4（同页 5 条 gold chunk
  受 max_per_page=2 约束）——这是设计行为，不是失败；但它说明**不开 pin
  时多样性机制本身就会稀释锚点覆盖**。真正的失败 onset：
  - 25 sources：1/3 reps 中承载事实的 gold chunk 被高分近义干扰挤出，
    facts 代理降到 0.5（该 rep "decoder" 缺失）；
  - 100 sources：1/3 reps 出现 **gold 全灭**（0/5），锚点完全丢失。
- 因此 pin 关时不稳定区间为 **25–100 sources（间歇性事实丢失），
  ≥100 sources 出现整锚点丢失**；pin 开则在测试范围内无失败。
  该区间是 pseudo-source 设定下的下界，真实语料预计更早。

## 8. 冻结用例

`benchmarks/context_cases.jsonl`：12 条（CTX-T01..T12），覆盖五个决策分支、
锚点保留对比、隔离、压缩红线、压缩 lineage、预算模型校验、tokenizer
一致性、无静默截断。`tests/context/test_benchmark_cases.py` 校验其结构
与分支覆盖。

## 9. 与上游的现状差距（后续 milestone 接线项）

- `radiant_llm.py:669-682` 仍是 `chars//4` + 70%/85% 硬编码阈值 +
  无证据保留保证的 overflow 压缩；本库提供 `TokenCounter` /
  `BudgetConfig` / `ContextEngine` 作为替换件。
- 接线时需把 M4 pipeline 的 `final` 候选映射为 `EvidenceItem`
  （score/page/authority），并把 gold anchor（retrieval_cases 的
  gold_anchor 解析结果）标记 `pinned=True`。
