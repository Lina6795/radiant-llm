# TALK_TRACKS：3 分钟项目介绍 + 15 分钟深挖讲稿

> 所有数字与 `docs/FINAL_EXPERIMENTS.md`、`docs/RESUME_BULLETS.md` 一致，可互查。讲的时候只讲有 artifacts 背书的句子。

## 3 分钟版（电梯陈述）

**（30 秒｜问题）**
RADIANT-LLM 是一个核工程领域的 Agentic RAG 开源实现，配论文的那种。我把它解包审计后发现：论文宣称引用强制、上下文管理、可靠决策，但代码里——所有请求直接进 LangChain AgentExecutor，没有 checkpoint，引用只靠提示词里一行字，token 预算是字符数除以四的粗估，检索是单路 dense。模型再强，这套工程壳子也不可靠。

**（60 秒｜我做了什么）**
我没动它的解析管线和模型，而是在上面新建了控制面和数据面两层。控制面：router → planner → schema guard → policy → durable runtime，计划必须过授权才能执行，执行全程有 checkpoint、幂等账本和 append-only 事件流，可以取消、可以从断点恢复。数据面：证据库按内容哈希加解析器指纹做幂等摄取，检索升级为 BM25+dense 的 RRF 融合加权威门控，上下文按精确 token 预算分配且关键证据可 pin 住不被挤掉，答案逐条 claim 过证据校验，低置信进人工 Review 队列。

**（60 秒｜结果，全部实测）**
四组对照：检索 Recall@5 从 0.234 到 0.299、证据冲突判定 3 例清零、逐用例无退化；中断恢复率 8/8——基线是零，因为旧系统根本没有恢复这个概念；提交答案的 unsupported rate 从 0.425 降到 0——这里我必须如实说明：收益来自人工 review 拦截弃答，不是模型变聪明了，这一点我在报告里专门写了口径声明；上下文压力测试 1 到 100 个来源，pin 住的关键证据保留率 100%，不 pin 掉到 40%。另外我做了一个解析对照实验：同一篇论文、同一个视觉模型，Nougat 路径保住 33 个公式块，PyMuPDF 降级路径一个都没有——这就是为什么证据身份必须带解析器指纹。

**（30 秒｜工程闭环）**
全部六层 116 个用例一条命令回归，release gate fail-closed，有一个真实失败用例我如实记红没有标绿。交付是 FastAPI + SSE + 运维 Dashboard，一键脚本在无 GPU 的云容器里原生跑起来。上游、fork 既有增量、我的新增三方归属写在 OWNERSHIP 文档里，论文的数字我一个都没有往自己身上揽。

## 15 分钟版（深挖讲稿）

### 1. 问题（2 分钟）

- 起点：上游 RADIANT-LLM 镜像（arXiv:2604.22755 配套），本机 Docker 不可用，逐层解包 + SHA256 校验后原生跑起来（fork 既有工作）。
- M0 审计结论（`docs/BASELINE.md`）：论文 §4 的所有数字不可复现——评分 YAML、benchmark 问题集、250 篇语料都不在镜像里。所以本项目一切指标自测自证，论文数字只作背景。
- 四个工程缺口坐实：无控制层（`radiant_llm.py:2027` 直进 AgentExecutor）、无 checkpoint（重试只覆盖 context overflow）、纯 dense top-20、citation 提示词级 + chars//4 预算。外加会话历史无门控直接注入、零测试零评测。

### 2. 上游缺陷实证（2 分钟）

- 纯 dense 实测：A0 recall@5=0.2344，16 条里 3 条 grader 判证据冲突——"检索到了但互相矛盾"是真实存在的失败模式。
- 无预算语义保护的实测：pin=off 时 gold 证据保留率 0.4，100 源时有一个 run 掉到 0.0——关键证据被填充文本挤掉，且旧系统对此完全无感。
- citation 提示词级实测：B0 单 Agent unsupported rate 0.476——将近一半的 claim 没有证据支撑，系统照样输出。
- 无 checkpoint：这不是"分数低"，是"指标的分母不存在"——架构性缺口，我在对照表里如实写架构性 not_measured 而不是编一个 0 分实验。

### 3. 控制面设计（4 分钟）

- 分层原则：控制面决定"能不能做、怎么恢复"，数据面决定"拿什么证据、敢不敢说"。两层只过 pydantic 严格契约（`extra="forbid"`）。
- 关键决策逐个讲（每个都带"放弃了什么"，见 `docs/ARCHITECTURE.md` §5）：
  1. 规则化 planner，物理上无法触达工具 handler——放弃灵活性换可审计；
  2. 自写状态机零新依赖——环境装不了 langgraph，顺手换来显式状态表；
  3. 证据永不压缩，pin 被挤就 abstain——放弃表面召回换事实不漂变；
  4. content_hash + parser_fingerprint 双键证据身份（ADR 0002）——同名不同内容、同字节不同解析栈都能区分，重复摄取短路为零。
- Durable Runtime：快照与事件分离、lease + fencing token、配置指纹不匹配拒恢复。坦白崩溃模拟是"新连接重开同一 SQLite"的语义等价方案，没真 kill——这是方法学声明不是隐瞒。

### 4. 六组对照实验（4 分钟）

按 `docs/FINAL_EXPERIMENTS.md` 顺序过，每张表只讲一个 from→to 和一个诚实脚注：

1. 检索：Recall@5 0.234→0.299，paired diff 无退化；A1 朴素 hybrid 先伤 3 条、RRF 追回——"为什么不是简单 hybrid"的直接证据；proxy rerank 无收益被我删了。
2. 上下文：pin on/off × 1–100 源 × 3 reps，gold 保留 1.0 vs 0.4；answer proxy 的口径局限我也写了。
3. Durable：8/8 vs 架构性缺失；恢复延迟 0.5–2.7s。
4. Memory：六指标全达标，基线是无治理的架构性 not_measured；正则清单的局限列出。
5. 核验：unsupported 0.425→0.000 的拦截口径声明——这是全场最重要的一张表，讲清楚"收益来自拦截不是修复"，fact_recall 降到 0.167 是代价，修订重生成是下一步。
6. 视觉对照（M10 新做）：同 PDF 同 VLM 只换 text_mode——公式 33 vs 0、图 8 vs 7（含一例真实 VLM JSON 解析失败丢图，没修，记着呢）；两条已产出路径 visual_qa 都是 0 缺陷，差异在覆盖率和文本质量。

### 5. 飞轮与 gate（2 分钟）

- 六层 116 用例单命令 223.6s；release gate fail-closed：基线测过本次没测=fail，完整性层 skip=fail。M8 实测人为注入退化后 gate 正确 fail 两条规则。
- Bad-case Registry + trace：BL-T05 真实未命中如实记红；失败用例进台账驱动下一轮（注入样本扩充、reranker 复测都是飞轮候选）。
- Dashboard 演示入口：`docs/DEMO_SCRIPT.md`（摄取幂等 → 计划被拒 → 故障恢复 → Review approve 恢复原 run → 触发回归对比基线）。

### 6. 限制与下一步（1 分钟）

- 限制：无 GPU（Nougat 41s/页、reranker 未测）、语料 1 篇 PDF、benchmark 自构、LLM 端点非严格确定单次运行、前端无源码、Docker 路径后置、CoP 语义档 not_measured。
- 下一步：带 verifier 反馈的修订重生成回路；真 cross-encoder 复测；1000 篇语料的三个已知瓶颈（dense 构建耗时、证据仲裁策略、解析异步化）；视觉 region 级证据（需要 bbox，上游不支持）。
