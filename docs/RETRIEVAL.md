# M4 — Retrieval & Evidence Control

日期：2026-09-22。范围：`app/retrieval/`（新增包）、`benchmarks/retrieval_cases.jsonl`（冻结用例）、`tests/retrieval/`、`artifacts/retrieval/m4-20260922/`（实验产物）。不改动 M1–M3 的任何模块。

## 管线

```
question
  └─ recall: BM25 (EvidenceStore 权威 text 证据)  ∥  dense (本地 Chroma + bge-base-en-v1.5)
  └─ fusion: round-robin interleave (A1) 或 RRF k=60 (A2+)
  └─ filters: workspace / document_version / valid window / authority_level / modality / degraded
  └─ rerank (可选，默认关): token-overlap PROXY
  └─ gates: Relevance Gate (绝对阈值/相对落差) + Anchor/Authority Gate
  └─ grader: enough | retrieve_more | conflict | abstain
```

每个阶段在 trace 中记录：候选数、头部分数、被过滤候选及原因、锚点保护动作、阶段延迟与总延迟、配置 fingerprint（配置 dict 的 SHA-256 前 16 位）。`trace.final[]` 里每条保留证据带 `source_ranks`（bm25/dense 各自的原始 rank 与分数）、`filter_reasons`、`gate_notes` —— 每条证据都能解释"从哪路召回、为何保留/被降权/被提升"。

## 模块与配置项

| 模块 | 职责 | 关键配置 |
|---|---|---|
| `bm25.py` | rank_bm25 索引，仅非 degraded text 证据；可 `rebuild` | `top_k` |
| `dense.py` | 薄封装 Chroma `similarity_search_with_score`；chroma `chunk_id` → `evidence_id` 映射（page+document_id 兜底）；保留原始排名 | `RADIANT_VECTOR_STORE`、`k` |
| `fusion.py` | RRF / round-robin interleave / dense-priority merge；每路原始 rank/score 存入 `source_ranks` | `rrf_k`（默认 60） |
| `filters.py` | 元数据过滤，逐条记录过滤原因 | `FilterConfig` |
| `rerank.py` | Cross-Encoder 风格接口 + 开关；**仅附带 deterministic proxy** | `RerankConfig(enabled=False)` |
| `gate.py` | Relevance Gate + Anchor/Authority Gate | `min_score_ratio`、`protect_top_k`、`authority_weights` |
| `grader.py` | 规则版充分性判定 | `min_evidence`、`min_top_score`、冲突 Jaccard 阈值 |
| `pipeline.py` | 编排 + trace + A0–A4 预设 | `RetrievalConfig` |
| `experiment.py` | A0–A4 对照实验运行器 | CLI/env，见下 |

## Proxy reranker 声明

`TokenOverlapReranker` 是 **deterministic proxy，不是 cross-encoder**：对候选内容做 IDF 加权 token 重叠打分（IDF 由候选集合自身估计，长度归一）。不下载、不加载任何模型。目的只是让 A3 消融能检验"rerank 阶段接线"在 CPU 环境下的行为与收益。真正的 cross-encoder 可实现同一 `Reranker` 协议插入，本仓库不附带。

## Anchor/Authority Gate 语义

当请求带有目标锚点（document + page/figure/chunk）时，若候选池中存在锚点匹配的权威证据但被宽泛高分 chunk 挤出 `protect_top_k` 窗口，gate 将其钉回窗口内并记录 `anchor_protected` 笔记；同时按 `authority_weights`（primary 1.0 / secondary 0.9 / tertiary 0.8 / unknown 0.7）加权重排。**评测中锚点来自 case 的 `gold_anchor`（仅用于度量）；生产中锚点应来自请求上下文（打开的文档、引用页、会话焦点），绝不来自 gold 标签。**

## 冻结用例

`benchmarks/retrieval_cases.jsonl`：16 条（≥12），12 条由 `baseline_cases.jsonl` 改造（`RET-*` 前缀、`source: baseline:BL-*`），4 条新增（`source: manual-m4`，覆盖 N=6 层数、Adam 优化器、FFN 公式、h=8/d_k=64，含 chunk 级锚点），2 条 refusal（`gold_anchor: null`，检验 grader 的 abstain/retrieve_more 行为）。

**锚点口径**：`page` 一律为 evidence.db 中的 evidence 页码（解析器页码），与 baseline PDF 页码存在 +1 左右的偏移；冻结前已逐条用 SQL 核对 gold 页上确有支撑内容（如 3.2.1 在 evidence page 4、Table 2 BLEU 在 page 8）。

## 指标口径

- 相关集：gold 文档（artifact_uri 文件名 → document_id）且 evidence page == gold page 的全部 text 证据。
- `recall@K` = |相关集 ∩ top-K| / |相关集|（集合召回，gold 页通常含多个 chunk）。
- `anchor_hit@K` = top-K 内是否命中任一相关证据（二值）。
- `MRR` = 1 / 首个相关证据 rank；`nDCG@20` 二值增益。
- 延迟 = 单条 case 的 pipeline 端到端 wall time（含两路召回；embedding 模型加载在计时外），P50/P95 跨 16 条 case。

## 一键复现

```bash
cd /mnt/lina/radiant-llm && \
  PYTHONPATH=app \
  RADIANT_EVIDENCE_DB=artifacts/baseline/m0-20260922/evidence.db \
  RADIANT_VECTOR_STORE=artifacts/baseline/m0-20260922/output/local_vector_store \
  HF_ENDPOINT=https://hf-mirror.com \
  ./runtime/bin/python3.12 -m retrieval.experiment \
    --output-dir artifacts/retrieval/m4-20260922
```

产物：`metrics.json`（各配置聚合指标 + fingerprint）、`paired_diff.json`、`cases/<config>/<case_id>.json`（每配置每 case 完整 trace）。

单测：`./runtime/bin/python3.12 -m pytest tests/retrieval -q`（45 条；离线合成数据为主，真实库 smoke 在 artifacts 缺失时 skip）。

## A0–A4 结果

语料：119 条权威 text 证据（单文档），14 条带锚点用例 + 2 条 refusal；recall_k=20。

| 配置 | Recall@5 | Recall@20 | anchor hit@5 | anchor hit@20 | MRR | nDCG@20 | P50 ms | P95 ms |
|---|---|---|---|---|---|---|---|---|
| A0 纯 dense top-20 | 0.2344 | 0.4292 | 0.9286 | 1.0 | 0.7792 | 0.4462 | 48.6 | 52.3 |
| A1 BM25+Dense（交错合并） | 0.2226 | 0.4513 | 0.8571 | 1.0 | 0.7673 | 0.4598 | 50.2 | 54.6 |
| A2 A1+RRF(k=60) | 0.2487 | 0.4775 | 1.0 | 1.0 | 0.8452 | 0.4888 | 51.4 | 64.1 |
| A3 A2+proxy rerank | 0.2226 | 0.4775 | 0.8571 | 1.0 | 0.8226 | 0.4787 | 50.5 | 62.2 |
| A4 A3+gates | 0.2985 | 0.4720 | 1.0 | 1.0 | 0.8571 | 0.5010 | 56.0 | 310.0 |

（anchor hit@20 全配置均为 1.0：小语料下 gold 页进 top-20 无区分度；区分度体现在 @5、MRR、nDCG 与 paired diff。A4 P95 高系单条离群 case，P50 与各配置相当。）

## Paired diff 结论

按"首个相关证据 rank"逐 case 配对（improved/worse/unchanged）：

- **A0→A1**：improved [RET-T05]；worse [RET-T01, RET-T04, RET-V01]；unchanged 10。朴素交错合并把 BM25 候选插进头部，稀释了 dense 头部质量。
- **A1→A2（RRF）**：improved [RET-T01, RET-T04, RET-T05, RET-V01]；worse []；unchanged 10。RRF 修复全部 3 个回退并保留 T05 的收益 —— 双路融合必须用分数融合而非位置交错。
- **A2→A3（proxy rerank）**：improved []；worse [RET-T04, RET-T05]；unchanged 12。proxy 单调有害。
- **A3→A4（gates）**：improved [RET-T04, RET-T05]；worse []；unchanged 12。Anchor Gate 把被 proxy 挤出去的锚点证据钉回 top-5，恰好修复 A3 造成的两处回退。
- **A0→A4（总体）**：improved [RET-T04, RET-T05, RET-V01]；worse []；unchanged 11。MRR 0.779→0.857，Recall@5 0.234→0.299，nDCG@20 0.446→0.501，无任一 case 变差。

## Proxy reranker 收益结论

**明确无收益：A2→A3 有 2 条 case 变差、0 条变好，anchor hit@5 1.0→0.857，MRR 0.845→0.823。** token 重叠 proxy 偏向长且字面重叠高的 chunk，会把真正承载答案的简洁定义句压下去。建议：当前形态下**删除 A3 阶段（默认保持 `rerank.enabled=False`，A4 的 gates 直接作用于 A2 输出即获得全部收益）**；如未来引入真正的 cross-encoder，再以同一 `Reranker` 协议替换并复测 A2→A3。接口与开关保留在 `rerank.py` 中，proxy 实现已明确标注非 cross-encoder。

## 偏差与风险

- 语料极小（单文档 119 chunk），指标绝对值仅供配置间相对比较，不可外推。
- A4 anchor gate 在评测中使用 gold anchor，存在度量性泄漏；生产中需替换为请求上下文锚点（见上）。A4 的 anchor_hit 提升应理解为"保护机制有效"而非检索变强。
- 规则版 grader 无法识别"语料中本就没有答案"：两条 refusal case（A100 延迟、英中 BLEU）在全部配置下被判为 `enough`（词面上确实存在高分 chunk）。abstain 语义需在 M5 接入 answer 层拒答策略后闭环，当前仅"无任何候选存活"时触发。
- dense 分数为 Chroma L2 距离经 `1/(1+d)` 单调映射到 (0,1]；BM25/dense/RRF/proxy 各阶段量纲不同，Relevance Gate 的绝对阈值只在单一分数体系下有意义（A4 用相对比例 `min_score_ratio`）。
- 延迟为共享 CPU 上单次测量，P95 存在离群（A4 的 310ms 系单 case）；跨配置比较以 P50 为准，各配置 P50 均在 ~50ms。
- chunk 级锚点（RET-N01/N12/T13/N14）当前度量仍按 page 级相关集计算，chunk 命中可从 trace 中复查。
- chroma telemetry 在本环境报错（`capture() takes 1 positional argument`）为上游版本噪声，不影响结果；曾尝试以 client_settings 关闭，但 langchain_chroma 在传 client_settings 时会忽略 persist_directory 返回空库（已在 `dense.py` 注释并加实验探针防回归）。
