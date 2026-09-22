# RADIANT-Control 架构说明

> 日期：2026-09-22 ｜ 配套：`docs/OWNERSHIP.md`（三方归属）、`docs/BASELINE.md`（改造前 as-is）、ADR 0001/0002

## 1. 背景与上游

本项目在上游 **RADIANT-LLM**（论文 arXiv:2604.22755 的配套 Docker 镜像 `zev94/radiant-llm`，逐层解包）之上做增量改造。上游提供：FastAPI 对话后端、LangChain AgentExecutor 主链路、13 个工具、三分离 PDF 解析管线（Nougat 文本/公式 + VLM 图表描述 + PyMuPDF 降级）、Chroma 纯 dense 检索、会话 JSONL 持久化、React 前端产物。

M0 审计（`docs/BASELINE.md` §4）确认的关键工程缺口：**无控制层**（所有 query 直进 AgentExecutor）、**无 checkpoint/状态机/幂等**、**纯 dense top-20 单路召回**、**token 预算靠 `chars//4` 粗估**、**citation 只有提示词自觉**、**会话历史无写入门控**、**零测试零评测**。

RADIANT-Control（M1–M9）的回答：保留上游解析管线不动，在其上新建 **控制面（Control Plane）+ 数据面（Data Plane）** 两层。

## 2. 目标架构

```mermaid
flowchart TB
    subgraph CP[控制面 Control Plane]
        direction LR
        RTR["router<br/>意图路由/注入拦截"] --> PLN["planner<br/>结构化计划(只产 raw dict)"]
        PLN --> GRD["schema_guard<br/>参数/依赖校验"]
        GRD --> POL["policy<br/>workspace 授权/风险分级"]
        POL --> SCH["scheduler / DurableRunner"]
        SCH -. "waiting_review" .-> RQ["Review Queue<br/>人工复核(决策不可变)"]
        RQ -. "approve→resume" .-> SCH
    end

    subgraph RUN[durable runtime (app/durable)]
        CKP["checkpoint 快照"] --- EVT["append-only 事件流"]
        LSE["lease + fencing token"] --- IDP["幂等账本"]
    end

    subgraph DP[数据面 Data Plane]
        direction TB
        ING["evidence 摄取<br/>content_hash+parser_fingerprint 幂等"] --> EDB[("evidence.db")]
        EDB --> RET["retrieval<br/>BM25+dense→RRF→Authority Gate"]
        RET --> CTX["context engine<br/>分项 token 预算/pin 保护"]
        MEM["memory<br/>Write/Read Gate, workspace 隔离"] --> CTX
        CTX --> GEN["LLM 生成(上游 Agent 链路)"]
        GEN --> VER["verification<br/>Claim-Evidence 校验/拒答"]
    end

    SCH --> RUN
    SCH --> RET
    SCH --> MEM
    VER --> RQ

    subgraph OBS[观测与评测]
        TRC["trace / bad-case registry"] --- EVL["eval runner + release gate<br/>六层 116 用例"]
    end
    CP -.-> OBS
    DP -.-> OBS
```

分层原则：**控制面决定"能不能做、按什么顺序做、出问题怎么恢复"；数据面决定"拿什么证据、放多少进上下文、答案敢不敢说"**。两层之间只通过结构化契约通信（`extra="forbid"` 的 pydantic 模型），控制面物理上无法触达工具 handler（planner 只见 registry 元数据）。

## 3. 目录映射

| 目录 | Milestone | 职责 | 关键文件 |
|---|---|---|---|
| `app/control/` | M2 | 意图路由、计划、schema guard、policy、budget、registry、scheduler | `models.py`（契约）、`router.py`、`planner.py`、`schema_guard.py`、`policy.py` |
| `app/durable/` | M3 | 持久执行状态机：checkpoint、append-only 事件、重试、幂等、lease、cancel/resume | `runner.py`、`checkpoint.py`、`events.py`、`idempotency.py`、`lease.py` |
| `app/evidence/` | M1 | 三分离 JSONL → 权威证据库（SQLite），文档级幂等摄取 | `adapter.py`、`store.py`、`models.py` |
| `app/retrieval/` | M4 | BM25 + dense 双路 → RRF 融合 → Relevance/Anchor/Authority Gate → Sufficiency Grader | `fusion.py`、`gate.py`、`grader.py`、`pipeline.py`、`experiment.py` |
| `app/context/` | M5 | tiktoken 精确预算、六分区配额、pin 保护、五分支决策（abstain>clarify>retrieve_more>compress>assemble） | `engine.py`、`budgets.py`、`selector.py`、`compressor.py` |
| `app/memory/` | M6 | MemoryCandidate + Write/Read Gate + 五分类 + supersede + TTL + workspace 隔离 | `write_gate.py`、`read_gate.py`、`supersede.py`、`metrics.py` |
| `app/verification/` | M7 | 原子 claim 拆分、claim-evidence 校验、拒答、Review Queue、visual QA | `claims.py`、`verifier.py`、`review.py`、`visual_qa.py` |
| `app/eval/` | M8 | 数据集注册表、六层执行器、论文五指标代码化、release gate、配置指纹 | `runner.py`、`registry.py`、`metrics.py`、`release_gate.py` |
| `app/observability/` | M8 | 统一 trace、Bad-case Registry | `trace.py`、`bad_cases.py` |
| `app/api.py` + `app/dashboard-static/` | M9 | `/runs`、`/reviews`、`/benchmarks`、SSE 事件流、运维 Dashboard | `api.py`（+M9 段）、`dashboard-static/` |
| 上游保留 | — | 解析管线（`app/utils/vp_*`）、对话链路（`radiant_llm.py`）、工具（`app/tools/`） | 见 `docs/OWNERSHIP.md` A 类 |

## 4. 核心数据契约

| 契约 | 定义处 | 要点 |
|---|---|---|
| RouterDecision / ExecutionPlan / PolicyDecision / RunSummary | `app/control/models.py` | pydantic `extra="forbid"`；ReasonCode 稳定枚举；fail-closed |
| StepState / RunState / ResumeInfo / TypedError | `app/durable/`（`docs/STATE_MACHINE.md`） | 快照与事件分离；事件 run 内单调 seq；配置指纹不匹配拒恢复（CheckpointMismatchError） |
| Document / Page / Figure / Evidence | `app/evidence/models.py`（`docs/EVIDENCE_SCHEMA.md`） | 证据身份 = `content_hash + parser_fingerprint`（ADR 0002）；degraded 显式标记、默认排除 |
| MemoryCandidate / WriteOutcome | `app/memory/models.py` | 模型只能提候选；Write Gate fail-closed，11 类机器可读拒绝原因 |
| Claim / ClaimEvidenceMap / VisualIssue | `app/verification/` | degraded 证据永不绑定 claim；review 决策不可变、可逐位 replay |
| EvalReport / ConfigFingerprint | `app/eval/` | 分母 0 → None；无 judge 显式 not_measured 禁止满分；gate fail-closed |

## 5. 关键设计决策与取舍

1. **控制面独立于模型能力（M2，ADR 0001）**：router/planner/guard/policy 全部规则化，planner 只产出 raw plan dict 且物理上无法 import 工具 handler。取舍：放弃 LLM 自由规划的灵活性，换取 100% 可离线复现的授权语义（冻结契约集上的拦截结论不随模型漂移）。
2. **证据身份 = content_hash + parser_fingerprint（ADR 0002）**：同名不同内容 → 新版本；同字节不同解析栈（Nougat vs lightweight）→ 不同版本，互不覆盖。取舍：多版本并存多占存储，换摄取真幂等（重复摄取 `new_records=0`）与可回放审计。详见表 6 的实证价值。
3. **自包含状态机而非 langgraph（M3）**：环境无 langgraph 包，手写显式状态/迁移表（`docs/STATE_MACHINE.md`），零新依赖。崩溃恢复用"同进程新连接重开同一 SQLite"语义等价模拟，而非真 kill——方法学声明在 `docs/FAILURE_RECOVERY.md`。
4. **Reranker 只留协议（M4）**：实测 deterministic proxy rerank 无收益（paired diff 2 变差 0 变好），**结论性删除 A3 阶段**，gates 直接作用于 RRF 输出；接口保留待真 cross-encoder（无 GPU/无模型预算）。这是"用实验砍掉自己代码"的案例。
5. **证据压缩是红线（M5）**：evidence 分区永不压缩（压缩即 `EvidenceCompressionError`），证据只能整条保留/带 reason 舍弃；pin 证据被挤时 abstain 而不是静默截断。取舍：牺牲"硬塞进上下文"的表面召回，换事实不漂变。
6. **收益来自拦截而非修复（M7，如实声明）**：B3 臂的 unsupported_rate 0.425→0.0 是 review 拦截弃答的结果（6/12 草稿被拒），fact_recall 同步下降——系统"更敢说不敢答"而非"更会答"。修订重生成回路是明确的下一步。
7. **模型可替换是架构约束（ADR 0001）**：DeepSeek 对话 + 本地 bge embedding + deepseek-flash 视觉的混合 provider 组合即实证；Control Plane 不绑定任何单一 provider。

## 6. 部署形态

单进程 FastAPI（`app/api.py`）+ SSE 事件流 + 静态 Dashboard，`./start_radiant.sh -d` 一键启动；全部状态落 4 个 SQLite（evidence / durable / review / queue）+ `artifacts/`，`deploy/backup.sh`、`deploy/restore.sh` 做整机备份恢复。无特权云容器原生运行（6 核 CPU / 30G 内存 / 无 GPU），Docker 路径为后续封装阶段（本机 Docker 不可用，见 ADR 0001）。
