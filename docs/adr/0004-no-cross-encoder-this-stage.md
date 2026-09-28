# ADR 0004：不采用 Cross-Encoder Rerank（本阶段）

- 日期：2026-09-24
- 状态：已接受
- 关联：V3 计划第 10 节 S3-8；`app/retrieval/rerank.py`（proxy 实现）；`artifacts/control-v3/S3/baseline/rerank-s38.json`

## 背景

S3-8 要求"先 benchmark 再决定是否引入真实 Cross-Encoder；无净收益则写'不采用'的 ADR"。
V3 全局红线：未加载真实模型并完成对照前，不得出现 Cross-Encoder Rerank 的简历表述。

## 实测事实

1. **真实 Cross-Encoder 无法 benchmark**：HF 缓存只有 `BAAI/bge-base-en-v1.5`（dense 嵌入）与 `facebook/nougat-small`（视觉解析）；环境 `HF_HUB_OFFLINE=1`，下载新模型不可用。按 V3 第 5.5-3 条，需要新增外部资源时如实记录而非绕过。
2. **代理 reranker 的对照结果**（同语料、同 top_k=5、8 个冻结 case，`rerank-s38.json`）：
   - no_rerank：hit@1 4/8、hit@5 8/8、MRR 0.6458
   - proxy_rerank：hit@1 4/8、hit@5 8/8、MRR 0.6771（Δ +0.0313）
   - 样本仅 8 case、单文档语料，+0.03 MRR 在噪声范围内，不构成净收益证据。

## 决定

1. **不引入真实 Cross-Encoder**（本阶段）：模型不可得且无法 benchmark；简历/文档中**禁止**出现"已实现 Cross-Encoder Rerank"表述。
2. **proxy reranker 不接入生产 `evidence.search`**：收益在噪声内，且它是 token-overlap 代理，接入生产会让 trace 里出现容易被误读为真实 rerank 的阶段。它继续保留在离线 `app/retrieval` 实验链中，`proxy_notice` 明确标注身份。
3. 生产检索链保持：BM25 + Dense + RRF + metadata gate + relevance/sufficiency gate（S3-3～S3-5 已实现）。

## 重新评估条件（满足其一即重开）

- 环境中获得可加载的真实 Cross-Encoder 模型（含版本与来源核验），并用同一冻结 case 集完成 no-rerank / proxy / real 三方对照；
- 冻结 case 集扩大到多文档、多来源语料（≥30 case），使 MRR 差异超过种子间方差。

## 影响

- 生产检索无 rerank 阶段；trace 的 `lanes.fused` 即最终排名。
- S3 阶段门"未加载真实模型时不出现 Cross-Encoder 简历表述"以此 ADR 为凭据。
