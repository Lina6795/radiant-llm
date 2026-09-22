# OWNERSHIP：代码归属与贡献边界

> M0 交付物 ｜ 2026-09-22 ｜ 本文件必须在每次发布/简历使用前复核更新

## 三方归属

### A. 上游（论文作者团队 / zev94 镜像）

- `app/` 全部原始代码：FastAPI 后端、LangChain agent 主链路、13 个工具、Visual-Parser 解析管线（vp_*）、Chroma 检索、会话管理、Grace vLLM 客户端、React 前端产物；
- `runtime/` 解释器与依赖；
- `Docker_Executable/` 部署定义；
- 论文（`paper.md`）与全部论文实验数字。

许可状态：镜像内**无独立 LICENSE**。在作者明确许可前，本项目仓库不重新分发镜像本体，仅以"解包自用+增量改造"方式使用；简历与对外材料中明确标注上游来源与论文引用（arXiv:2604.22755）。

### B. fork 既有增量（仓库所有者，RADIANT-Control 计划之前）

| 内容 | 位置 | 说明 |
|---|---|---|
| 领域技能库 | `app/radiant_llm_skills/` | Tier-0 策略 + 6 个 Tier-1 包、hybrid 路由、两级注入扫描 |
| MANIFEST 签名 | `maintenance_scripts/generate_skill_manifest.py`、`app/utils/skill_manifest.py` | SHA-256 完整性校验 |
| 无 Docker 原生运行 | `start_radiant.sh`、`runtime/`、`部署记录-无Docker运行.md` | 镜像层解包 + 校验 + 原生启动 |
| 混合模型接入（DeepSeek 对话 + 本地 embedding） | `app/radiant_llm.py` deepseek 分支 | 2026-09-22 M0 新增 |

### C. RADIANT-Control 增量（本项目核心贡献，M0 起）

| 内容 | 位置 | 状态与真实指标 |
|---|---|---|
| 环境修复：HF 变量名兼容、HF 镜像、LangSmith 条件化、tesseract 接入、agent 预算配置化 | `vp_nougat_engine.py`、`radiant_llm.py`、`tools/*.py`、`utils/general_utilities.py`、`start_radiant.sh` | M0 完成；Nougat CPU 实跑 15 页 615.4s、119 chunk 全 `extractor=nougat`；端到端 5/5（`artifacts/baseline/m0-20260922/`） |
| 基线分析书 / 上游审计 / 本文件 / ADR 0001/0002 | `docs/BASELINE.md`、`docs/UPSTREAM_AUDIT.md`、`docs/adr/` | M0 完成 |
| Evidence 权威库与幂等摄取（M1） | `app/evidence/`（models/adapter/store）、`docs/EVIDENCE_SCHEMA.md` | 证据身份 content_hash+parser_fingerprint；首次摄取 121 条，重复摄取 `new_records=0`；`tests/evidence` 15 测试 |
| Control Plane 契约（M2） | `app/control/`（router/planner/schema_guard/policy/budget/registry/scheduler/models）、`docs/CONTROL_CONTRACT.md` | fail-closed 全链路；`tests/control` 49 测试；冻结集 router 12 条、policy 16 条（M8 回归 28/28） |
| Durable Runtime（M3） | `app/durable/`（graph/checkpoint/events/retry/idempotency/lease/runner）、`docs/STATE_MACHINE.md`、`docs/FAILURE_RECOVERY.md` | checkpoint/append-only 事件/lease+fencing/cancel/resume；恢复率 8/8=1.0；`tests/durable` 46 测试；冻结集 RT-01..08 |
| Hybrid 检索（M4） | `app/retrieval/`（bm25/dense/fusion/filters/rerank/gate/grader/pipeline/experiment）、`docs/RETRIEVAL.md` | A0→A4：recall@5 0.2344→0.2985、anchor_hit@5 0.9286→1.0、grader conflict 3→0、paired diff 无变差；A3 proxy rerank 实测无收益已结论性删除；`tests/retrieval` 47 测试；`artifacts/retrieval/m4-20260922/` |
| Context 预算与 pin（M5） | `app/context/`（tokenizer/budgets/selector/compressor/isolate/engine/experiment）、`docs/CONTEXT_BUDGET.md` | 1–100 sources 压力：pin=on gold 保留 1.0（全梯度），pin=off 0.4（100 源出现 0.0）；evidence 永不压缩；`tests/context` 44 测试；`artifacts/context/m5-20260922/` |
| Governed Memory（M6） | `app/memory/`（models/write_gate/read_gate/supersede/store/metrics）、`docs/MEMORY_GOVERNANCE.md` | 六指标：write precision 1.0、Recall@5 1.0、stale 0.0、conflict recall 1.0、泄漏 0、无 provenance 0.0；`tests/memory` 68 测试 |
| Claim 校验与人工 Review（M7） | `app/verification/`（claims/claim_map/verifier/review/visual_qa/visual_eval/experiment）、`docs/CLAIM_VERIFICATION.md` | B0–B3 实测：提交答案 unsupported 0.425→0.0、citation precision 0.5→1.0（**收益来自拦截弃答而非修复**，fact_recall 同步下降，如实声明）；`tests/verification`+`tests/review` 47 测试；`artifacts/verification/m7-20260922/` |
| Eval Harness + Release Gate + 观测（M8） | `app/eval/`（registry/adapters/runner/metrics/release_gate/fingerprint）、`app/observability/`（trace/bad_cases）、`docs/EVAL_HARNESS.md` | 六层 116 用例单命令回归（111 pass/1 fail 如实记红/4 skip），gate 自比 pass、人为退化正确 fail；`tests/eval` 95 测试；`artifacts/eval/m8-baseline-20260922/` |
| API + SSE + Dashboard + 原生交付（M9） | `app/api.py`（/runs /reviews /benchmarks /documents/ingest、SSE）、`app/dashboard-static/`、`deploy/backup.sh`、`deploy/restore.sh` | `./start_radiant.sh -d` 一键启动 + 健康轮询；演示脚本 `docs/DEMO_SCRIPT.md`；`tests/api` |
| 冻结 benchmark 与评测产物 | `benchmarks/`（9 个 jsonl，SHA-256 见 M8 report `data_versions`）、`artifacts/` | M0 起步，M8 冻结基线 |
| 最终实验与求职材料（M10） | `docs/FINAL_EXPERIMENTS.md`、`docs/ARCHITECTURE.md`、`docs/INTERVIEW_QA.md`、`docs/RESUME_BULLETS.md`、`docs/TALK_TRACKS.md`、`artifacts/final/` | 六组对照表定稿；Nougat vs lightweight 视觉对照实测（`artifacts/final/m10-vision-compare.json`） |

**测试总量（M1–M9 新增）**：`tests/` 下 evidence 15 + control 49 + durable 46 + retrieval 47 + context 44 + memory 68 + verification/review 47 + eval 95 + api（M9）= 见各 milestone 报告；全量命令 `./runtime/bin/python3.12 -m pytest tests -q`。

## 使用规则

1. 简历与 README 中的每一条能力声明，必须能归入 B 或 C 并给出 commit/文件证据；A 类能力只能表述为"基于上游 RADIANT-LLM"。
2. 论文实验数字（CoP/CiP/CiH/HR/ViR 任何值）永远是 A 类，不得引用为本项目结果。
3. 对上游代码的任何修改（包括 M0 环境修复），在 commit message 中注明 `[upstream-mod]`，便于将来生成纯净 diff。
