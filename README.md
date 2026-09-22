# RADIANT-LLM

面向核工程 RAG 的研究助手：文档摄取（PDF → 证据库）、检索问答、Agent 控制平面（router→planner→guard→policy→durable runner）、人工 Review 队列与评测回归。本文件只覆盖**原生（无 Docker）运行路径**；Docker 封装为后续阶段。

## 背景与归属

本仓库是上游 **RADIANT-LLM**（论文 arXiv:2604.22755 配套 Docker 镜像，逐层解包）的增量改造：**RADIANT-Control** 在其多模态解析管线之上新建 Agent 控制面与数据面。三方归属严格分清——上游镜像与论文数字（A）、fork 既有增量如技能库（B）、本项目新增的控制面/持久执行/检索/上下文/记忆/核验/评测包（C）——见 [docs/OWNERSHIP.md](docs/OWNERSHIP.md)；上游能力与缺陷的逐节审计见 [docs/BASELINE.md](docs/BASELINE.md)、[docs/UPSTREAM_AUDIT.md](docs/UPSTREAM_AUDIT.md)。**论文实验数字（CoP/CiP/CiH/HR/ViR）在本仓库不可复现，仅作背景引用，不是本项目成果。**

## 架构一段

控制面（`app/control/`：route→plan→guard→authorize；`app/durable/`：checkpoint/事件/幂等/恢复；`app/verification/`：claim 核验 + 人工 Review）决定"能不能做、出问题怎么恢复"；数据面（`app/evidence/` 幂等摄取、`app/retrieval/` RRF+Gate、`app/context/` 精确预算+pin、`app/memory/` 读写门控）决定"拿什么证据、敢不敢说答案"；`app/eval/` + `app/observability/` 提供六层回归与 release gate。分层图、目录映射、数据契约与关键取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/adr/](docs/adr/)。

## 六组对照实验摘要

全部数字出自真实运行，每张表的数据版本/样本数/口径/失败 case 见 [docs/FINAL_EXPERIMENTS.md](docs/FINAL_EXPERIMENTS.md)（2026-09-22，6 核 CPU / 30G / 无 GPU）：

| 对照 | from → to | 证据 |
|---|---|---|
| Dense → RRF+Gate（16 冻结用例） | Recall@5 0.234→0.299，锚点@5 0.929→1.000，冲突 3→0 | `artifacts/retrieval/m4-20260922/` |
| 无 pin → Budget/Pin（1–100 源 ×3 reps） | gold 证据保留 0.4→1.0（100 源基线现 0.0） | `artifacts/context/m5-20260922/` |
| 无 Checkpoint → Durable Runtime | 恢复率 架构性缺失 → 8/8=1.0 | `benchmarks/runtime_cases.jsonl`、M8 durable 层 |
| 会话注入 → Governed Memory | 泄漏 0、write precision 1.0、stale 0.0 | `tests/memory/test_metrics.py` |
| B0 单 Agent → B3 Verifier+Review | 提交答案 unsupported 0.425→0.000*（*拦截口径，见表 5 声明） | `artifacts/verification/m7-20260922/` |
| Nougat vs lightweight 视觉（同 PDF 同 VLM） | 公式 chunk 33 vs 0；图描述 8 vs 7（1 例真实 VLM 失败） | `artifacts/final/m10-vision-compare.json` |

完整性背书：六层 116 用例单命令回归（111 pass / 1 fail 如实记红 / 4 skip），基线 `artifacts/eval/m8-baseline-20260922/`。

## 限制

- 无 GPU：Nougat CPU ≈41s/页；cross-encoder rerank 未测（proxy 版实测无收益已删除）。
- 语料规模小：1 篇公开 PDF / 119 条权威 evidence；benchmark 为自构冻结集。
- LLM 端点非严格确定：B0–B3 为单次运行，无跨次方差。
- 视觉语义正确性 not_measured（无 region 金标）；CoP 语义档 not_measured（需 judge/人审）。
- 前端 React 无源码（镜像只有 dist）；本机 Docker 不可用，交付为无特权容器原生运行。
- B3 的收益来自拦截弃答而非修复生成（fact_recall 同步下降），修订重生成回路未实现。
- 已知失败 case 与逐项原因：[docs/FINAL_EXPERIMENTS.md](docs/FINAL_EXPERIMENTS.md)、[docs/INTERVIEW_QA.md](docs/INTERVIEW_QA.md) Q6。

求职材料：[docs/RESUME_BULLETS.md](docs/RESUME_BULLETS.md)、[docs/TALK_TRACKS.md](docs/TALK_TRACKS.md)、[docs/INTERVIEW_QA.md](docs/INTERVIEW_QA.md)；演示：[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)。

## 快速开始（原生路径）

### 1. 环境前提

- Linux + bash、`curl`、`tar`。
- 仓库内置 Python 解释器 `runtime/bin/python3.12`（自带全部依赖：FastAPI 0.115 / pydantic 2.12 / pytest 9.1.1 等），**不要**用系统 Python；所有命令里的 `./runtime/bin/python3.12` 都是它。
- 本机无需 Docker。

### 2. 最小配置 `.env`

```bash
cp Docker_Executable/.env.example Docker_Executable/.env
```

`start_radiant.sh` 启动时自动 source 该文件。最小可用配置只需：

```dotenv
# 对话 LLM（DeepSeek，OpenAI 兼容协议）
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
# 证据库（指向已有 M0 基线；留空则默认仓库根 evidence.db）
RADIANT_EVIDENCE_DB=/mnt/lina/radiant-llm/artifacts/baseline/m0-20260922/evidence.db
```

模型说明：

- **对话/文本**：`deepseek-v4-pro`（通过 `OPENAI_BASE_URL` + `OPENAI_API_KEY` 走 DeepSeek）。
- **视觉解析**（PDF 图表描述、文档元数据）：`deepseek-flash`（多模态）。摄取 API `/documents/ingest` 可用 body 里的 `vision_model` 指定；缺省走解析管线的环境配置。

### 3. 启动与健康检查

```bash
./start_radiant.sh -d        # 后台启动，并自动轮询 /health（最长 120s）报告结果
./start_radiant.sh status    # 随时查看进程 + 健康状态
./start_radiant.sh stop      # 停止
```

健康检查通过后，服务在 `http://127.0.0.1:8080`（`RADIANT_LLM_PORT` 可改）：

```bash
curl http://127.0.0.1:8080/health    # {"status":"ok"}
```

- **Operations Dashboard**：<http://127.0.0.1:8080/dashboard/>（Runs / Evidence / Reviews / Benchmarks，纯静态页，无需构建）
- 对话前端（React dist）：<http://127.0.0.1:8080/>
- API 文档（OpenAPI）：<http://127.0.0.1:8080/docs>

日志：`radiant-llm.out.log`（超过 50MB 自动轮转，保留 5 份；`LOG_MAX_MB`/`LOG_KEEP` 可调）。

### 4. 一个 Run 的最小验证

```bash
curl -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"goal": "search evidence and export report", "workspace": "default"}'
# => {"run_id": "...", "status": "running", ...}
curl http://127.0.0.1:8080/runs/<run_id>          # 节点状态/尝试次数/配置指纹
curl -N http://127.0.0.1:8080/runs/<run_id>/events  # SSE 事件流
```

新 API 全量契约见 [docs/API.md](docs/API.md)，演示脚本见 [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)。

### 5. 备份与恢复

```bash
./deploy/backup.sh                     # 打包 4 个 SQLite + artifacts/ 到 backups/
./deploy/restore.sh backups/<文件>.tar.gz   # 校验后恢复（需输入 yes 确认；-y 跳过）
```

## 测试

```bash
./runtime/bin/python3.12 -m pytest tests -q          # 全量
./runtime/bin/python3.12 -m pytest tests/api -q      # M9 API 层
```

## 评测回归（M8）

```bash
PYTHONPATH=app ./runtime/bin/python3.12 -m eval.runner --all \
  --out artifacts/eval/<run_id> --baseline artifacts/eval/m8-baseline-20260922
```

也可在 Dashboard 的 Benchmarks 页或 `POST /benchmarks/run` 触发。

## 目录速览

| 路径 | 说明 |
|---|---|
| `app/api.py` | FastAPI 入口（对话 + 控制平面 API） |
| `app/control/` `app/durable/` | M2 控制平面契约 / M3 持久执行运行时 |
| `app/evidence/` `app/retrieval/` | 证据库（SQLite 幂等摄取）/ 检索 |
| `app/verification/` | 声明核验与 M7 人工 Review 队列 |
| `app/eval/` | M8 评测框架与 release gate |
| `app/dashboard-static/` | M9 运维 Dashboard（原生 JS，无构建） |
| `deploy/` | 备份/恢复脚本 |
| `docs/` | 契约与设计文档（API、状态机、评测等） |
