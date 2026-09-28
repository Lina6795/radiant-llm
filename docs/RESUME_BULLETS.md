# RESUME_BULLETS：简历项目条目终稿

> 规则：每条后面附证据路径；口径有坑的地方用脚注如实声明；答不出三层追问的数字已删除（`docs/INTERVIEW_QA.md`）。

## 项目条目（可直接贴简历）

**RADIANT-Control：安全关键技术文档 Agent 控制面** ｜ 个人项目 ｜ 2026.09
*基于上游 RADIANT-LLM（arXiv:2604.22755 配套实现）增量构建；Python / FastAPI / SQLite / SSE*

- **项目背景**：针对上游 RAG Agent 在多源检索稳定性、工具调用治理与长任务恢复上的工程缺口（M0 代码审计坐实：纯 dense 单路召回、无 checkpoint、citation 仅提示词级、token 预算 chars//4 粗估），在其多模态解析管线之上设计并实现控制面/数据面分层架构。
  证据：`docs/BASELINE.md` §4；`docs/ARCHITECTURE.md`

- **执行控制**：设计 `route → plan → guard → authorize → execute → verify → review` 工作流，拆分 Router、Schema Guard、Policy Engine 与 Durable Runtime（checkpoint/append-only 事件/lease/幂等账本/cancel/resume），planner 物理上无法触达工具 handler，全链路 fail-closed；中断恢复成功率 **8/8 = 1.0**（基线：上游无 checkpoint，恢复概念不存在）。
  证据：`app/control/`、`app/durable/`；`benchmarks/runtime_cases.jsonl`；`artifacts/eval/m8-baseline-20260922/` durable 层；`docs/STATE_MACHINE.md`

- **证据与检索治理**：构建 BM25 + Dense → RRF → Authority Gate 混合检索，在 16 条冻结用例上将 Recall@5 从 **0.234 提升至 0.299**、锚点命中@5 从 0.929 提至 1.000、证据冲突判定从 3 例降至 0 例，逐用例 paired diff 无退化；实测砍掉无收益的 proxy rerank 阶段。上下文侧以 tiktoken 分项预算 + 证据 pin 替换 chars//4 粗估，1–100 源压力梯度下关键证据保留率 **1.0**（无 pin 基线 0.4，100 源时跌到 0.0）。
  证据：`artifacts/retrieval/m4-20260922/metrics.json`、`paired_diff.json`；`artifacts/context/m5-20260922/metrics.json`

- **治理与评测**：实现 Memory 写入门控（注入/秘密/无溯源 11 类机器可读拒绝）与 workspace 隔离——write precision 1.0、过期命中 0、冲突检出 1.0、**跨 workspace 泄漏 0**（口径：workspace 逻辑隔离，非用户身份认证）；写入路径为显式 API + 回答 accept 后自动写 session 摘要/证据指针（user_fact/decision 需人工确认）。实现 claim-evidence 核验与人工 Review 队列，提交答案的 unsupported rate 从 0.425 降至 **0.000***（*口径：B3 仅统计实际提交的 6/12 答案，收益来自拦截弃答而非修复生成，fact_recall 同步下降，如实声明）。建成六层 **116 用例单命令回归**与 fail-closed release gate（111 pass / 1 fail 如实记红 / 4 skip）；S10 起 E2E 层判定 accept/review 分列，review 不计 pass。
  证据：`tests/memory/test_metrics.py`；`artifacts/verification/m7-20260922/summary.json`；`artifacts/eval/m8-baseline-20260922/report.json`

- **交付与实测对照**：FastAPI + SSE + 静态运维 Dashboard，`./start_radiant.sh -d` 一键启动，在无特权云容器（6 核 CPU / 30G / 无 GPU）原生运行；备份/恢复脚本覆盖全部 SQLite 与 artifacts。完成 Nougat vs PyMuPDF 降级路径视觉对照实测：公式保留 33 chunk vs 0、图描述 8 vs 7（含 1 例真实 VLM 输出解析失败，未修复如实记录），论证证据身份必须携带 parser 指纹。
  证据：`README.md`；`deploy/backup.sh`、`deploy/restore.sh`；`artifacts/final/m10-vision-compare.json`；`docs/adr/0002-evidence-identity.md`

## 数据速查（面试随手引用）

| 数字 | 口径 | 证据 |
|---|---|---|
| Recall@5 0.234→0.299 | 16 冻结用例，A0 dense → A4 RRF+Gate，离线确定性 | `artifacts/retrieval/m4-20260922/metrics.json` |
| 恢复率 8/8=1.0 | RT-01..08 冻结运行时场景；基线架构性缺失 | `benchmarks/runtime_cases.jsonl`、M8 durable 层 |
| unsupported 0.425→0.000* | 12 冻结答案用例，B1→B3；*拦截口径见脚注 | `artifacts/verification/m7-20260922/summary.json` |
| gold 保留 1.0 vs 0.4 | 1–100 源 × pin on/off × 3 reps，16k 窗口 | `artifacts/context/m5-20260922/metrics.json` |
| 泄漏 0 / precision 1.0 | 合成治理套件，pytest 断言即门禁 | `tests/memory/test_metrics.py` |
| 116 用例单命令回归 | 六层，223.6s，1 fail 如实记红 | `artifacts/eval/m8-baseline-20260922/report.md` |
| 公式 chunk 33 vs 0 | 同一 PDF 同一 VLM，仅 text_mode 不同 | `artifacts/final/m10-vision-compare.json` |

## 不写上简历的（及原因）

- 论文的 CoP/CiP/CiH/HR/ViR 任何数字——A 类上游成果，仓库内无数据不可复现（`docs/BASELINE.md` §2.7）。
- "100% 拦截注入"之类的绝对化表述——仅限冻结契约测试集（`docs/CONTROL_CONTRACT.md` 措辞红线）。
- B3 的 claim_support 0.800、citation precision 1.000 单独引用——必须连带拦截口径声明，否则构成误导。
- 视觉语义正确率——无 region 金标，not_measured。
