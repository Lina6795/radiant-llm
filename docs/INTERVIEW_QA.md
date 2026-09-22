# INTERVIEW_QA：防伪审计六问 + 预期深挖

> 用途：面试/评审时逐条自证。每条答案都附真实证据路径；答不出三层追问的术语和数字已从简历中删除或降级。
> 配套：`docs/FINAL_EXPERIMENTS.md`（六张表）、`docs/OWNERSHIP.md`（三方归属）、`docs/BASELINE.md`（改造前 as-is）。

## 防伪审计六问

### Q1. 旧方案具体哪里失败？

四处失败全部有改造前代码证据 + 改造后对照实验：

1. **纯 dense 单路召回**：上游 `similarity_search(k=20)`（`app/utils/pdf_helpers.py:236-250`），无 BM25/RRF/rerank。实测 A0 recall@5=0.2344、grader conflict 3/16（`artifacts/retrieval/m4-20260922/metrics.json`）。
2. **无 checkpoint/状态机**：上游 LangChain `AgentExecutor` 单次执行，重试只覆盖 context overflow（`app/radiant_llm.py:2092-2155`）。长任务一断全丢——架构性缺口，连"恢复率"这个指标的分母都不存在（`docs/BASELINE.md` §4）。
3. **citation 靠提示词自觉**：论文宣称引用强制，代码里只有系统提示词一行文字（`system_prompt_radiant_llm.yml:113`），无结构化引用、无逐句校验、无拒答逻辑代码化。实测 B0 unsupported_rate=0.476（`artifacts/verification/m7-20260922/summary.json`）。
4. **token 预算 `chars//4` 粗估 + 无证据保护**：`radiant_llm.py:669-682` 注释自承 "rough"；上下文溢出时无 pin 语义，关键证据可被任意挤掉。实测 pin=off 时 gold 证据保留率从 1.0 掉到 0.4，100 源时出现 0.0 的 run（`artifacts/context/m5-20260922/metrics.json`）。

另有两处审计发现但本期未做对照实验：会话 JSONL 无写入门控直接注入（`session_store.py:246-327`，M6 治理）；PythonREPLTool 无沙箱（未改造，列入限制）。

### Q2. 你修改了哪个模块和哪个关键接口？

**原则：上游解析/对话链路基本不动，新增六个包，接口即数据契约。**

| 改动 | 模块 | 关键接口 |
|---|---|---|
| 证据层（新） | `app/evidence/` | `EvidenceStore.ingest_directory()`：幂等键 `(workspace_id, document_id, content_hash, parser_fingerprint)`，重复摄取短路 `new_records=0`（ADR 0002） |
| 控制面（新） | `app/control/` | `RouterDecision → ExecutionPlan → PolicyDecision` 三段式契约（`models.py`，pydantic `extra="forbid"`）；planner 只产 raw plan dict，物理上无法触达工具 handler |
| 执行层（新） | `app/durable/` | `DurableRunner.run/resume/cancel`；`CheckpointStore`（快照）与 `EventStore`（append-only，run 内单调 seq）分离；配置指纹不匹配抛 `CheckpointMismatchError` 拒恢复 |
| 检索（新） | `app/retrieval/` | `pipeline.run(query) → ranked evidence + grader verdict`；RRF(k=60) + Relevance/Anchor/Authority Gate |
| 上下文（新） | `app/context/` | `ContextEngine.assemble()` 五分支决策（abstain>clarify>retrieve_more>compress>assemble）；evidence 分区压缩直接抛 `EvidenceCompressionError` |
| 记忆（新） | `app/memory/` | 模型只能提 `MemoryCandidate`，`WriteGate.evaluate()` fail-closed（11 类机器可读拒绝原因）；`ReadGate.recall()` 强制 workspace/过期/superseded 过滤 |
| 核验（新） | `app/verification/` | `ClaimEvidenceMap` 逐 claim 绑定/拒绑；Review Queue 决策不可变、`replay()` 可逐位重放 |
| 评测（新） | `app/eval/` + `app/observability/` | `eval.runner --all` 单命令六层回归；`release_gate` fail-closed |
| 上游修改（仅环境修复） | `vp_nougat_engine.py`（HF token 变量名兼容）、`radiant_llm.py`（DeepSeek 分支、LangSmith 条件化） | commit message 均带 `[upstream-mod]`，可生成纯净 diff |

### Q3. 为什么选择该方案，放弃了什么？

1. **规则控制面 vs LLM 自由规划**：选规则 router/planner/guard/policy。放弃规划灵活性，换来授权语义可离线 100% 复现、拦截结论不随模型漂移——安全场景里"可审计"比"聪明"重要。
2. **自写状态机 vs langgraph**：环境无 langgraph 包且不能装（零新依赖约束），手写显式状态/迁移表（`docs/STATE_MACHINE.md`）。放弃生态，换确定性与可审计。
3. **RRF+Gate vs cross-encoder rerank**：无 GPU、无模型预算。实测 deterministic proxy rerank 无收益（paired diff 2 变差 0 变好）后**结论性删除 A3 阶段**——用实验砍掉自己的代码，接口保留待真 reranker 复测（`artifacts/retrieval/m4-20260922/`）。
4. **证据永不压缩 vs 硬塞上下文**：evidence 分区只允许整条保留/带 reason 舍弃，pin 证据被挤时 abstain 而非截断。放弃表面召回率，换事实不漂变。
5. **Review 拦截弃答 vs 自动修复重生成**：M7 的 B3 收益（unsupported 0.425→0.0）来自拦截而非修复，修订重生成回路未做——如实声明为下一步，不包装成"解决了幻觉"。
6. **content_hash+parser_fingerprint 双键身份 vs 单 document_id**：同名不同内容、同字节不同解析栈都必须可区分（ADR 0002）。代价是多版本并存多占存储。

### Q4. Baseline、数据集和指标口径是什么？

- **Baseline 两类**：(a) 真实可跑基线——A0 纯 dense（上游 Chroma 路径原样保留为对照臂）、pin=off、B0 单 Agent HTTP 全链路；(b) 架构性 not_measured——无 checkpoint、无 memory 治理这两项旧系统没有对应对象，如实表述为"缺口"而非"0 分"（`docs/FINAL_EXPERIMENTS.md` 表 3/4 脚注）。
- **数据集**：9 个冻结 jsonl（`benchmarks/`），SHA-256 逐文件记录在 `artifacts/eval/m8-baseline-20260922/report.json` `data_versions`；语料为 1 篇公开 PDF 解析出的 119 条权威 evidence。规模小是事实，所有结论限定在该冻结集上。
- **指标口径**：claim 级指标在各自臂自身检索证据上测（B0 例外，在共享 BM25 池上测，已声明口径差异）；escalation 金标由 B2 校验输出确定性派生而非人工标注；`unsupported_rate` 等论文五指标为本项目重新实现的严格口径（分母 0 返回 None、无 judge 显式 not_measured），**与论文数字无任何关系**。
- **运行次数**：M4/M5/M6 为确定性离线实验（M5 每格 3 reps 有 pstdev）；M7 B0–B3 各 1 次（LLM 端点非严格确定，无跨次方差，标 not_measured）。

### Q5. 哪些代码来自上游镜像，哪些是 fork 既有增量，哪些是本次新增？

三方分清，详见 `docs/OWNERSHIP.md`：

- **A 上游（论文作者团队镜像）**：`app/` 原始代码（FastAPI 后端、LangChain 链路、13 工具、vp_* 解析管线、Chroma 检索）、`runtime/` 解释器、`Docker_Executable/`、论文及全部论文实验数字。论文数字（CoP/CiP/CiH/HR/ViR 任何值）永远只作背景引用。
- **B fork 既有增量（改造前已完成）**：领域技能库（`app/radiant_llm_skills/`）、MANIFEST 签名、无 Docker 原生运行方案（`start_radiant.sh`、`部署记录-无Docker运行.md`）。
- **C 本项目新增（M1–M10）**：`app/evidence/`、`app/control/`、`app/durable/`、`app/retrieval/`、`app/context/`、`app/memory/`、`app/verification/`、`app/eval/`、`app/observability/`、`app/dashboard-static/`、全部 `tests/`、`benchmarks/`、`artifacts/`、`docs/` 契约文档。对上游的修改仅 M0 环境修复，commit 带 `[upstream-mod]`。

### Q6. 失败 case 和项目限制是什么？

**真实失败 case（未修复，如实记录）**：

1. BL-T05 检索未命中 top-20（M0 锚点偏移，M8 回归如实记 fail、runner exit 1）——`artifacts/eval/m8-baseline-20260922/report.md` retrieval 层。
2. lightweight 解析 p6 的 VLM 返回 JSON 解析失败，丢 1 张图描述（Table 1），figures 8→7——`artifacts/final/m10-lightweight-run.log` WARNING ×2。
3. M5 pin=off 100 源 run3 gold 保留 0.0；25 源 rep2 answer proxy 0.5（decoder 事实丢失）——`artifacts/context/m5-20260922/metrics.json`。
4. B3 的 fact_recall 降到 0.167——拦截弃答的代价，系统"更不敢答"而非"更会答"（表 5 声明）。

**项目限制**：

- 无 GPU：Nougat CPU ≈41s/页，100-PDF 真实语料压力实验不可行（M5 用 pseudo-sources，方法学局限已声明）；cross-encoder rerank 未测。
- 语料规模小：1 篇 PDF / 119 条 evidence；benchmark 为自构冻结集，不是公开 benchmark。
- 前端 React 无源码（镜像只带 dist），不重写；Dashboard 走独立静态页。
- 本机 Docker 不可用：交付形态为无特权容器原生运行 + 备份恢复脚本，Docker compose 封装后置。
- LLM 端点非严格确定：B0–B3 单次运行，无跨次方差。
- PythonREPLTool 无沙箱：本期未改造，policy 层按高风险工具管控（外部 review）。
- CoP 语义档 not_measured（需 judge/人审，`docs/EVAL_HARNESS.md`）。

## 预期深挖十问

**D1. RRF 为什么比朴素 hybrid 好？**
A1（top-k 内交错合并）实测伤 3 个用例：BM25 高分词面噪声挤掉 dense 锚点（anchor competition）。RRF 按排名倒数融合、不比较异质分数绝对值，A1→A2 paired diff 4 变好 0 变差（`paired_diff.json`）。k=60 是标准值，未调参。

**D2. checkpoint 粒度怎么定的？恢复正确性怎么证明？**
节点级快照：节点成功才写 checkpoint；恢复时只重跑无成功 checkpoint 的节点，已提交副作用靠幂等账本（idempotency key）短路。证明：RT-01..08 冻结场景 8/8，含 lease 过期接管、fencing token 防双写、取消竞态（`benchmarks/runtime_cases.jsonl`、`docs/FAILURE_RECOVERY.md`）。

**D3. 崩溃恢复是真的进程崩溃吗？**
不是真 kill——用"同进程新连接重开同一 SQLite"语义等价模拟（WAL 模式下与进程崩溃后重开等价），方法学声明在 `docs/FAILURE_RECOVERY.md`。这是限制，不是隐瞒。

**D4. 配置指纹是什么？为什么恢复要校验它？**
git commit + python 版本 + 关键 env + 模型配置 + 数据集 SHA-256 的哈希（`app/eval/fingerprint.py`）。恢复的语义前提是"同一份代码和同一份配置继续跑"，指纹不匹配默认抛 `CheckpointMismatchError` 拒恢复——防止"换了模型/代码却接着旧 checkpoint 跑"的静默污染。

**D5. Memory 的 write precision 1.0 会不会是规则过严、什么都不让写？**
套件里 3 条合法写入（含 1 条未确认的 session 类）全部放行，8 条非法/攻击全部拦截，precision 按"放行中合法占比"计算=1.0 且合法召回未受损（`tests/memory/test_metrics.py` WRITE_SUITE）。11 类拒绝原因机器可读，不是黑盒。

**D6. 六层 116 用例回归要多久？怎么进 CI？**
223.6s 单命令（`eval.runner --all`），其中 verification 层 115s（真实 LLM 调用）。release gate fail-closed：基线已测本次未测→fail；完整性层 skip→fail。M8 实测人为注入退化（recall 0.431→0.25 + 置一个 fail）后 gate 正确 fail 两条规则（`docs/milestone_reports/M8.md` §6）。

**D7. 为什么 B0 延迟 22s 而 B2/B3 只要 4.6s？**
B0 走运行中 HTTP 服务全链路（含会话注入、技能路由、二次润色），B1–B3 是库级直跑。这同时说明控制面核验链路比上游对话链路轻一个量级，也说明 B0 与其他臂口径不同（summary.json notes 已声明）。

**D8. visual_qa 的规则会不会太弱，测不出 VLM 幻觉？**
它只测结构性缺陷（空描述/错页/错索引/低信息含零数字密度的"伪定量"声明），不测语义正确性——语义档需要 region 级金标，上游不给 bbox，所以视觉语义正确性是 not_measured 并列入限制。表 6 里两条路径 0 缺陷的意思是"结构与信息量合格"，不是"描述都对"。

**D9. 如果语料扩到 1000 篇，架构哪里先扛不住？**
三个已知点：(1) Chroma dense 检索在无 GPU 下的构建耗时；(2) M5 已证 100 源内预算/pin 机制成立，但 evidence 配额 8k token 下的多文档证据仲裁策略需要重调；(3) Nougat CPU 解析吞吐（≈41s/页）要求解析走异步批处理管线，目前是同步摄取。

**D10. 这个项目和"再写一个 LangChain agent"的区别是什么？**
上游本身就是 LangChain agent——本项目的论点是：在模型能力不变的前提下，控制面（授权/持久执行/证据治理/核验/回归）能系统性消除四类工程失败（表 1/3/5 的 from→to），且每一步都可离线复现、可审计。模型可以换（DeepSeek/Gemini/本地 embedding 已实证），控制语义不变。
