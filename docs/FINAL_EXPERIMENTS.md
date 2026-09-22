# FINAL_EXPERIMENTS：六组对照实验终稿

> 日期：2026-09-22 ｜ 铁律：每个数字都能引用到具体 artifacts 路径；没有的数据写 not_measured 及原因；论文数字永不写成本项目成果。

**公共实验条件**

- 硬件：无特权云容器，6 核 CPU / 30G 内存 / **无 GPU**（`docs/BASELINE.md` §1）
- 模型：对话/生成 `deepseek-v4-pro`（fallback `deepseek-flash`）；视觉 `deepseek-flash`（多模态，OpenAI 兼容端点）；embedding 本地 `BAAI/bge-base-en-v1.5`（CPU）；tokenizer `tiktoken-o200k`
- 语料：`attention_is_all_you_need_1706.03762.pdf`（15 页）解析产物 → M1 权威证据库 119 条 text evidence（`evidence.db`，`artifacts/baseline/m0-20260922/`、`artifacts/baseline/m0-vision-deepseek/`）
- 数据版本：各冻结集 SHA-256 见 `artifacts/eval/m8-baseline-20260922/report.json` `data_versions`（如 retrieval_cases `dd41fad8…`、answer_cases `321f284c…`）
- 运行日期：2026-09-22（单日完成；除标注 reps 外均为单次运行——LLM 输出非严格确定，指标为单次实测值，无跨次方差，方差格标 not_measured）
- 费用：M4/M5/M6 纯本地（0 API 费用）；M7 B1–B3 每臂 prompt 6566 token、completion 2354–2798 token（`artifacts/verification/m7-20260922/summary.json`）；B0 token not_measured（HTTP 端点不暴露 usage）

---

## 表 1：Dense vs Hybrid vs RRF vs Gate（M4）

- 产物：`artifacts/retrieval/m4-20260922/metrics.json`、`paired_diff.json`（80 条 per-case trace 在同目录 `cases/`）
- 样本：16 条冻结用例（`benchmarks/retrieval_cases.jsonl`，14 条带可解析锚点）；证据库 119 条 text evidence；recall_k=20
- 运行次数：每配置 1 次（离线确定性，无 LLM 参与）；日期 2026-09-22

| 配置 | recall@5 | recall@20 | anchor_hit@5 | MRR | ndcg@20 | p50/p95 延迟 ms | grader enough/conflict |
|---|---|---|---|---|---|---|---|
| A0 纯 dense（现有基线） | 0.2344 | 0.4292 | 0.9286 | 0.7792 | 0.4462 | 48.6/52.3 | 13/3 |
| A1 BM25+dense 合并 | 0.2226 | 0.4513 | 0.8571 | 0.7673 | 0.4598 | 50.2/54.6 | 12/4 |
| A2 RRF(k=60) | 0.2487 | 0.4775 | 1.0000 | 0.8452 | 0.4888 | 51.4/64.1 | 15/1 |
| A3 RRF+proxy rerank | 0.2226 | 0.4775 | 0.8571 | 0.8226 | 0.4787 | 50.5/62.2 | 16/0 |
| A4 RRF+Gates | **0.2985** | 0.4720 | **1.0000** | **0.8571** | **0.5010** | 56.0/310.0 | **16/0** |

Paired diff（逐用例，源 `paired_diff.json`）：

| 对比 | 变好 | 变差 |
|---|---|---|
| A0→A1 | RET-T05 | RET-T01, RET-T04, RET-V01 |
| A1→A2 | RET-T01, RET-T04, RET-T05, RET-V01 | 无 |
| A2→A3 | 无 | RET-T04, RET-T05 |
| A3→A4 | RET-T04, RET-T05 | 无 |
| A0→A4 | RET-T04, RET-T05, RET-V01 | 无 |

结论与失败 case：

- **A0→A4 recall@5 0.2344→0.2985（+0.064），anchor_hit@5 0.9286→1.0，grader conflict 3→0，且 paired diff 无任一用例变差。**
- A1 朴素合并反而伤 3 个用例（anchor competition 实证），RRF 融合全部追回——这是"为什么不是简单 hybrid"的直接证据。
- A3 proxy rerank 无收益（2 变差 0 变好），**结论性删除**（保留接口待真 cross-encoder）；不是失败 case 而是方法学决策。
- 失败/限制 case：M8 复跑时 BL-T05 真实未命中 top-20（M0 锚点偏移，`artifacts/eval/m8-baseline-20260922/report.md` retrieval 层如实记 fail）；A4 p95 延迟 310ms（gate 调 grader 的尾部成本）。
- 均值/方差：not_measured——单次确定性运行（无随机源），方差无意义；M8 独立复跑精确复现 A4 指标（recall@20 0.472、MRR 0.8571），佐证确定性。

## 表 2：无预算 vs Budget/Pin（M5 上下文压力）

- 产物：`artifacts/context/m5-20260922/metrics.json`、`REPORT.md`
- 样本：1 真实文档（119 chunks）+ 种子化近义/填充干扰源的 pseudo-sources（方法学局限已声明：真实 100-PDF 语料 Nougat CPU ≈17h 不可行）；16k token 窗口，6 分区配额；gold = RET-T01 锚点 5 chunks（facts: encoder/decoder）
- 运行次数：每格 3 reps（seed 20260922 逐位确定）；mean±pstdev；日期 2026-09-22
- 基线口径：pin=off 等价于上游"无预算语义保护"行为（chars//4 粗估 + 无 pin，证据可被任意挤掉）；pin=on 为本项目 Budget/Pin

| sources | pin | token 节省率 | gold chunks 保留 | answer proxy | 组装延迟 ms |
|---|---|---|---|---|---|
| 1 | on | 0.8071±0.0079 | **1.0±0** | 1.0±0 | 27.3±0.9 |
| 1 | off | 0.8209±0.0101 | 0.4±0 | 1.0±0 | 27.3±0.2 |
| 10 | on | 0.8349±0.0076 | **1.0±0** | 1.0±0 | 32.8±0.4 |
| 10 | off | 0.8447±0.0101 | 0.4±0 | 1.0±0 | 32.8±0.3 |
| 25 | on | 0.8687±0.0078 | **1.0±0** | 1.0±0 | 40.7±0.3 |
| 25 | off | 0.8912±0.0042 | 0.4±0 | 0.833±0.236 | 41.3±0.5 |
| 50 | on | 0.9116±0.0009 | **1.0±0** | 1.0±0 | 54.9±0.6 |
| 50 | off | 0.9249±0.0051 | 0.4±0 | 1.0±0 | 56.2±0.8 |
| 100 | on | 0.9390±0.0032 | **1.0±0** | 1.0±0 | 82.5±1.4 |
| 100 | off | 0.9424±0.0032 | 0.267±0.189 | 1.0±0 | 82.5±0.2 |

结论与失败 case：

- **pin=on 在 1–100 sources 全梯度 gold 保留 1.0；pin=off 稳定掉到 0.4，100 sources 时出现 0.0 的 run（gold 全丢）。** 失败 case：sources=25/pin=off rep2 answer proxy 掉到 0.5（decoder 事实丢失）；sources=100/pin=off rep3 gold 保留 0.0（answer proxy 仍 1.0 是因为该 proxy 只查 fact 词在不在，暴露了 proxy 口径的局限，如实记录）。
- 所有 30 个 run `drops_without_reason=0`、`n_compressions=0`（evidence 永不压缩红线成立）、残余溢出 0。
- 上游 chars//4 本身的精度对照：not_measured——M5 直接用 tiktoken 替换了估算路径，未保留双轨对照实验（替换属缺陷修复而非算法对比，见 `docs/CONTEXT_BUDGET.md`）。

## 表 3：无 Checkpoint vs Durable Runtime（M3）

- 产物：`tests/durable/`（46 测试）+ `benchmarks/runtime_cases.jsonl`（RT-01..08）+ `artifacts/eval/m8-baseline-20260922/` durable 层（`report.json`）
- 样本：8 条冻结运行时场景（恢复、取消、重试、幂等、lease 接管、并发）；M8 复跑同 8 条
- 运行次数：M3 连续 2 次全绿 + M8 回归 1 次；日期 2026-09-22

| 指标 | 基线（无 checkpoint，上游 AgentExecutor） | Durable Runtime |
|---|---|---|
| 中断后恢复成功率 | 0（架构性 not_measured*） | **8/8 = 1.0** |
| 重复副作用 | 无幂等机制 | 幂等账本拦截（effect_count 审计，M8 8/8 pass） |
| 取消语义 | 无 | 双通道 cancel，`cancelled` 终态 + 事件 |
| 断线重连 | 无 | append-only 事件流 + Last-Event-ID |

\* 基线 0 属"架构性未跑"：上游链路根本没有 checkpoint 概念，不存在可恢复的 run 对象，无法构造对照实验——如实表述为架构缺口（`docs/BASELINE.md` §4：`radiant_llm.py:2092-2155` 重试仅覆盖 context overflow），而非"跑了 8 次失败 8 次"。

- 恢复延迟（M8 durable 层 8 case）：517.7–2679.3 ms（RT-06 幂等短路 4.4ms）；均值/方差：not_measured（每 case 单次）。
- 失败 case：无（8/8 全过）；方法学声明：崩溃用"同进程新连接重开同一 SQLite"语义等价模拟，未真 kill 子进程（`docs/FAILURE_RECOVERY.md`）。

## 表 4：聊天记录 vs Governed Memory（M6）

- 产物：`tests/memory/test_metrics.py`（断言即门禁）+ `benchmarks/memory_cases.jsonl`（17 条）+ `docs/MEMORY_GOVERNANCE.md`；M8 回归 `artifacts/eval/m8-baseline-20260922/` memory 层（17/17 pass，write_precision 1.0）
- 样本：11 条写入套件（合法/注入/秘密/无 provenance/低置信/只读命名空间混合）+ 3 条冲突套件 + 9 条召回套件；测试内合成，确定性
- 运行次数：pytest 每次运行即复测（M6 时 68/68；M8 回归 17/17）；日期 2026-09-22

| 指标 | 基线（会话 JSONL 直接注入） | Governed Memory |
|---|---|---|
| write precision（非法/攻击写入零放行） | not_measured* | **1.0** |
| memory Recall@5 | not_measured* | **1.0**（门 ≥0.8） |
| stale hit rate（过期记忆命中） | not_measured* | **0.0** |
| conflict detection recall | not_measured* | **1.0** |
| 跨 workspace 泄漏数 | not_measured* | **0** |
| 无 provenance 记录比例 | not_measured* | **0.0** |

\* 基线为架构性 not_measured：上游会话存储（`session_store.py:246-327`）无 Write Gate/分类/workspace 概念，TF-IDF/fuzzy 检索结果直接注入 prompt——没有"写入门控"这个对象可以打分，六项指标的分母在旧系统里不存在（`docs/BASELINE.md` §4）。可定性引用的基线事实：旧路径对注入文本（如"记住：忽略之前的所有指令"）零拦截。

- 覆盖场景：同名实体隔离 / 纠正偏好 supersede / 文档事实冒充用户事实拒绝 / 11 条注入变体（中英）拦截 / 过期决策 TTL / 跨会话串味过滤。
- 失败 case / 限制：注入与秘密模式为正则清单，需随红队样本扩充；用户显式确认文本不再做注入拦截（确认优先，语义已文档化）；Read Gate 为确定性 token 重叠召回（非向量），接线时可加 embedding 但隔离/过期/superseded 过滤必须保留。

## 表 5：B0 vs B1/B2/B3（M7 答案核验）

- 产物：`artifacts/verification/m7-20260922/summary.json`（+`cases/` per-case、`work/` 中间产物）；方法学声明 `docs/CLAIM_VERIFICATION.md` §3–5
- 样本：12 条冻结 answer 用例（`benchmarks/answer_cases.jsonl`）；模型 `deepseek-v4-pro`；日期 2026-09-22；每臂 1 次（LLM temperature=0 但端点非严格确定，无跨次方差→not_measured）
- 口径声明：B0 走运行中 HTTP 服务、检索不可观测，claim 级指标在共享 BM25 池上测量（与 B1–B3 口径不同，直接对比需谨慎）；拒答用例 2/2 各臂均正确；escalation 金标由 B2 校验输出确定性派生（非人工标注）

| 指标 | B0 单 Agent(HTTP) | B1 +Planner/Tools | B2 +Verifier | B3 +Review Gate |
|---|---|---|---|---|
| claim_support_rate | 0.242 | 0.350 | 0.267 | 0.800* |
| citation_precision | 0.500 | 0.500 | 0.500 | 1.000* |
| citation_coverage | 0.524 | 0.575 | 0.700 | 1.000* |
| numeric_accuracy | 0.267 | 0.225 | 0.325 | 0.667* |
| unsupported_rate | 0.476 | 0.425 | 0.300 | **0.000*** |
| fact_recall | 0.808 | 0.458 | 0.408 | 0.167* |
| escalations (prec/rec) | 0 (—/0) | 0 (—/0) | 9 (1.0/0.818) | 8 (1.0/0.727) |
| latency_ms_mean | 22124 | 6928 | 4549 | 4646 |

\* **B3 拦截语义声明**：B3 的 claim 级均值只统计**实际提交**的答案——6/12 草稿被 scripted reviewer 拒绝后 run 取消、不提交。**unsupported 0.425→0.0、precision→1.0 的收益来自拦截弃答而非修复生成质量**，fact_recall 降到 0.167 是同一枚硬币的另一面（系统"更不敢答"，不是"更会答"）。修订重生成回路未实现，是明确的下一步。
- 失败 case：B0 延迟 22.1s vs B2/B3 ~4.6s（HTTP 全链路含二次润色）；B1 fact_recall 0.458 低于 B0 0.808——控制面收紧了证据口径后草稿更保守，如实记录。
- 视觉用例 AN-V01/V02：M7 时无 VLM key，答案仅来自文本证据（summary.json notes）；本次 M10 已补齐真实视觉产物（见表 6）。

## 表 6：Nougat vs lightweight 视觉对照（M10 本次实测）

- 产物：`artifacts/final/m10-vision-compare.json`（本表全部数字的单一来源）；lightweight 侧原始产物 `artifacts/final/m10-lightweight/`（output/ 三分离 JSONL + parse_run_summary.json + 运行日志 `artifacts/final/m10-lightweight-run.log`）；Nougat 侧 `artifacts/baseline/m0-vision-deepseek/`
- 样本：同一 PDF `attention_is_all_you_need_1706.03762.pdf`（15 页），同一 VLM `deepseek-flash`，同一 vision_provider="gpt" 兼容端点；唯一变量 `text_mode` = nougat vs lightweight（PyMuPDF 文本层）
- 运行次数：各 1 次；日期 2026-09-22；视觉 QA 工具 `app/verification/visual_qa.py`（规则版，确定性）
- 复现命令：`./runtime/bin/python3.12 artifacts/final/run_m10_lightweight.py`（env 从 `Docker_Executable/.env` 加载）

| 维度 | Nougat + deepseek-flash | lightweight(PyMuPDF) + deepseek-flash |
|---|---|---|
| 文本 chunk 数 | 119（extractor=nougat ×119） | 103（extractor=lightweight ×103） |
| chunk 长度 min/mean/median/max | 13/344.2/370/499（pstdev 135.3） | 98/441.8/456/500（pstdev 72.9） |
| 含 LaTeX 公式标记的 chunk 数 | **33** | **0** |
| 图描述数量 | 8（页 3,4,5,6,10,13,14,15） | 7（页 3,4,5,8,13,14,15） |
| 图描述平均长度（字符） | 2224.5 | 1977.0 |
| visual_qa 缺陷率 | 0/8（empty/wrong_page/wrong_index/low_info 均 0） | 0/7（同上） |
| 文档元数据 | 1 条（标题/作者/arXiv 号，权威） | 1 条（同） |
| 文本抽取耗时 | not_measured 分计* | **4.3s**（日志行） |
| 端到端总耗时（含 8/7 次 VLM 调用） | 393.5s | 230.5s |
| API 费用 | 视觉调用次数：8 图 + 1 元数据（token 数 not_measured——解析管线不记录 usage） | 7 图 + 1 元数据（同左） |

\* Nougat 侧文本抽取耗时在本 run 未单独计；M0 同 PDF 独立实测为 615.4s/15 页（`artifacts/baseline/m0-20260922/`，冷缓存），本次 393.5s 总耗时（含 VLM）小于该值，说明 Nougat 本次为暖缓存运行——不做精确分计，如实标注。

结论与失败 case：

1. **公式保留是两条路径的本质差异**：Nougat 路径 33/119 chunk 带 LaTeX 标记，lightweight 路径 0/103——对技术文档 Agent，lightweight 等于丢失全部结构化公式，这正是 ADR 0002 用 `parser_fingerprint` 区分两条解析栈、互不覆盖的实证理由。
2. **图描述覆盖不同且都有真实缺口**：两条路径喂给 VLM 的页面渲染/文本不同，检出的图集合不同（Nougat 多 p6/p10，lightweight 多 p8）；lightweight 侧 p6 的 VLM 返回 JSON 解析失败（日志 WARNING ×2），丢 1 张图（Table 1），figures 8→7——**真实失败 case，未修复、如实记录**。
3. **缺陷率对照**：已产出的图描述两侧均通过 visual_qa 全部规则检查（0 缺陷）；差异在**覆盖率**（8 vs 7）与**文本质量**（公式保留、chunk 长度分布），不在单条描述质量。
4. chunk 分布：lightweight 更长更均匀（mean 441.8 vs 344.2，pstdev 72.9 vs 135.3），因为 PyMuPDF 文本层没有 Nougat 的语义断句，定长 500/100 切块的填充率更高；Nougat 侧 13 字符的短 chunk 多为公式/标题碎片。
5. 速度：lightweight 文本抽取 4.3s vs Nougat 分钟级（615.4s 冷缓存实测），是"质量换速度"的明确 trade-off；对无 GPU 环境，lightweight 是可用的降级路径，但**必须带 extractor 溯源并在证据层区分权威级别**（M1 degraded 标记机制）。
6. 对照实验的已知局限：单一 PDF、单次运行、图集合差异部分来自 VLM 输入差异而非纯抽取器差异；`visual rerank/region evidence` 对照 not_measured——上游管线不产生 bbox/region 证据（`docs/EVIDENCE_SCHEMA.md` degraded 规则），无对象可测。

---

## 附：六层回归基线（M8，非对照实验但为一切表格的完整性背书）

`artifacts/eval/m8-baseline-20260922/report.json` / `report.md`：116 用例单命令回归（223.6s），111 pass / 1 fail（BL-T05 真实未命中，如实记红）/ 4 skip（无锚 refusal，记原因不算分母）；六层全 ok；release gate 自比 pass、人为退化注入后正确 fail。复现命令：`PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --all --out artifacts/eval/<run_id> --baseline artifacts/eval/m8-baseline-20260922`。
