# RADIANT-Control 逐阶段协作执行计划

> 用途：在现有 RADIANT-LLM 仓库上，逐步把已有的旁路控制模块接入真实 Agent 主链路。  
> 执行方式：用户 + Codex 共同理解、实现和验收；Kimi Code 只执行当前阶段的小任务卡。  
> 重要边界：本计划改造的主体是 **RADIANT-LLM**，不是另建 Visual-Parser 项目。仓库路径、端口、数据目录均由运行时配置确定，不在代码和计划中写死。

## 1. 我们现在在哪里

当前仓库并不是空项目，已经有：

- `control / durable / retrieval / context / memory / verification / eval` 分层；
- SQLite checkpoint、lease、retry、cancel、resume、idempotency 和事件流；
- BM25 + Dense + RRF + Gate 的离线检索管线；
- Context Budget、Memory Governance、Claim Verification 和 Eval Harness；
- 116 个 benchmark case 与 429 个通过的自动化测试。

但是当前有两条未合并的链路：

```text
真实问答：/stream-query → cb.convchain_api() → 原 LangChain Agent

新控制面：/runs → RuleRouter → RulePlanner → Guard/Policy
                     → DurableRunner → mock tools
```

所以项目当前的真实状态是：**控制组件基本齐全，但尚未接管 RADIANT-LLM 主链路。**

## 2. 本次执行协议

### 2.1 每次只做一个阶段

一个阶段内也不允许 Kimi Code 一次全部完成，而是按下面的小循环进行：

```text
1. 读代码，画出当前请求/数据/状态流
2. 用口语解释关键类、函数和数据结构
3. 先补一个能暴露缺口的失败测试
4. 实现最小改动
5. 运行定向测试和一个真实 smoke case
6. 审查 git diff，记录 before/after
7. 用户确认理解后，才决定是否进入下一小步
```

### 2.2 三种必须同时存在的证据

每个阶段只有同时具备下列证据才能完成：

1. **代码证据**：真实实现和可审查 diff；
2. **运行证据**：真实 API/CLI 调用经过该模块，trace 能看到；
3. **评测证据**：至少一个正常 case、一个失败 case 和对应指标。

只有单元测试、只有模块目录或只有离线脚本，都不算阶段完成。

### 2.3 每阶段的固定交付物

- `CURRENT_FLOW.md`：当前链路图与关键代码解释；
- `TASK_CARD.md`：本次只允许修改的文件与任务；
- 失败测试 → 修复后的测试记录；
- 一条可重放的真实 trace；
- `STAGE_REPORT.md`：before/after、失败 case、限制、下一步；
- 一段用户能自己复述的面试话术。

## 3. 阶段总览

| 阶段 | 控制面 | 主要目标 | 完成标志 |
|---|---|---|---|
| S0 | 基线与边界 | 冻结现状，清理运行风险，建立主链路图 | 可重现 baseline，无评测混淆 |
| S1 | 意图与工具控制 | 让 Router/Planner 操作真实 RADIANT 工具 | 去掉 mock search/report，有真实 ToolResult |
| S2 | 执行控制 | 让主查询经过 durable runtime | 真实 `/query` 可 checkpoint/resume/cancel |
| S3 | 检索控制 | 把混合检索接入真实回答链 | 主链路使用 BM25+Dense+RRF+Gate |
| S4 | 上下文控制 | 受控组装 Evidence/Memory/Artifact | 超预算可压缩且 gold 不丢 |
| S5 | 记忆控制 | 用可治理 Memory 替换纯聊天历史 | 无 workspace/跨会话串味 |
| S6 | 引用与回答控制 | Claim-Evidence 校验成为必经环节 | 不支持的 claim 不直接输出 |
| S7 | 视觉证据控制 | 建立图表/区域级证据与门控 | 真实视觉 case 可定位原页证据 |
| S8 | 评测、可观测与数据飞轮 | 用真实主链路自动回归 | Release Gate 对完整候选运行通过 |
| S9 | 端到端收口 | 统一 API/UI，去掉双链路 | 一条主链、一键 smoke、可演示 |

## 4. S0：基线与项目边界

### 目标

先把当前工程保护好，建立之后每个阶段的对照基线。

### 我们一起理解

- FastAPI 入口、`/stream-query`、`convchain_api` 的调用流；
- 原 Agent 的 tool list、memory、retriever 和 PDF pipeline；
- 新 `/runs` 控制面的数据流；
- 两条链路的交汇点与断点。

### 小任务

1. 保存 `git status`、commit、环境、模型配置和测试结果。
2. 定位并处理 core dump 的根因；未确认前不直接删除。
3. 区分源码、运行数据库、日志、artifact 和备份，补 `.gitignore` 建议。
4. 跑一次原问答 smoke、一次 `/runs` smoke 和一次全量测试。
5. 建立一个固定的 end-to-end case，以后每阶段都重放。
6. 审计代码来源：保留可证明的上游来源和修改边界；不因为代码来自镜像就未经核验地声称 Apache-2.0。

### 验收

- baseline 测试数和失败集已冻结；
- 可重复执行原问答和控制面 smoke；
- 当前主链路图能由用户自己讲出来；
- 未处理的工程风险全部记入台账。

### 停止点

S0 完成后停止，由用户回答：“一个问题从 API 到回答经过了哪些对象？”回答清楚后才进 S1。

## 5. S1：意图与工具控制

### 当前缺口

- Router 主要依赖英文正则和关键词；
- Planner 只能产生 `evidence.search` 和 `report.export` 两种 mock 计划；
- 13 个 RADIANT 原工具只注册元数据，没有 handler；
- `evidence.search` 使用三条 `_MOCK_DOCS`。

### 目标

让控制面可以安全地调用真实 RADIANT 能力，而不是继续增加 mock。

### 小任务

1. 定义第一批真实工具的 typed adapter：文档检索、证据详情、PDF 摄取、引用校验。
2. 为每个 adapter 定义 input schema、`ToolResult`、timeout、risk 和 typed error。
3. 用 adapter 包装原函数，不立即重写原 RADIANT 工具。
4. 让 Tool Registry 注册真实 handler，删除对 `_MOCK_DOCS` 的运行时依赖。
5. 将 Router 的意图扩展为中英文可测试的 `knowledge_qa / visual_qa / ingest / compare / audit`。
6. Planner 只生成结构化计划，并由 Schema Guard/Policy 拒绝未知工具、越权参数与不合法计划。

### 学习问题

- Router、Planner、Tool Registry 为什么不能合成一个大 Agent？
- Schema Guard 与 Policy Engine 各防什么问题？
- adapter 为什么比重写原工具更适合当前阶段？
- `retryable error` 和 `terminal error` 有什么区别？

### 验收

- 至少 3 个真实工具从 Registry 可执行；
- 一个真实 PDF 问题能返回非 mock Evidence ID；
- 未知工具、错误 schema、高风险操作均 fail closed；
- trace 记录 router decision、plan、policy verdict、tool version 和 result。

### 本阶段可用的简历表达

> 设计 Router–Planner–Schema Guard–Policy–Tool Registry 受控工具链，以 typed adapter 接入 RADIANT 真实 PDF 检索与证据工具，对未知工具、非法参数与高风险操作执行 fail-closed 控制。

## 6. S2：执行控制

### 当前缺口

DurableRunner 本身已比较完整，但它没有接管真实问答；计划还保存在进程内存中，完整重启后的恢复边界不完整。

### 目标

将原问答包装成真实 durable run，支持 checkpoint、retry、timeout、cancel、resume、lease 和幂等。

### 小任务

1. 明确自研 `StateGraph` 节点和转移，不冒称 LangGraph。
2. 设计 feature flag，让新链先只承接一类真实 query。
3. 将 plan JSON、workspace 和执行必要配置持久化，去除恢复对进程内存的依赖。
4. 把工具副作用与 idempotency key 绑定。
5. 做四类故障注入：timeout、transient error、进程中断、重复请求。
6. SSE 只从 append-only Event Store 发送，断线后可从 event offset 续传。

### 学习问题

- checkpoint 与普通日志有什么区别？
- at-least-once 执行为什么需要幂等？
- lease 解决什么并发问题？
- retry 为什么不能对所有错误生效？

### 验收

- 真实问答 run 在进程重启后可继续；
- 重复请求不产生重复副作用；
- cancel 不再启动新节点；
- timeout/retry 在 trace 中可见；
- 故障注入下记录恢复成功率、重复副作用数和事件丢失数。

## 7. S3：检索控制

### 当前缺口

离线 RetrievalPipeline 已实现，但原 `RetrievalOnlyUtility` 仍在主问答中使用旧向量检索。所谓 rerank 是 token-overlap proxy，并且实验中存在负收益。

### 目标

让真实回答统一通过 Hybrid Retrieval，并能解释每条证据为什么被保留或丢弃。

### 小任务

1. 将 Evidence Store 数据与原 Chroma 文档建立稳定 ID 映射。
2. 把 `RetrievalPipeline` 接入真实 search tool，替换主链旧 retrieval call。
3. 保留 BM25 + Dense + RRF，将 proxy reranker 默认关闭。
4. 增加 Metadata/Version/Workspace ACL Gate，确保过滤发生在证据进入 prompt 之前。
5. 先为真 Cross-Encoder 保留接口；引入前先做 CPU/显存/延迟预算。
6. 对固定数据集运行 A0/A1/A2/A4 消融和多次重复，不只报单次数字。

### 验收

- `/query` trace 明确出现 BM25、Dense、RRF、Filter、Gate 阶段；
- 每个候选有 rank、score、drop reason 和 config fingerprint；
- 报告 Recall@K、MRR、nDCG、Anchor Hit、p50/p95 与均值/方差；
- BL-T05 必须被解释或修复，不得从报告中删除；
- 引入 Cross-Encoder 时，必须证明相对 A2 的质量收益值得额外延迟。

## 8. S4：上下文控制

### 当前缺口

Context Engine 已有预算、选择和压缩实现，但主 Agent 仍主要靠字符估算、`ConversationBufferWindowMemory` 和异常后压缩。已有压力实验使用 pseudo-sources 和 simulated scores。

### 目标

在 LLM 调用前统一组装 system、active turn、memory、evidence、artifact 和 tool result，而不是超限后才被动补救。

### 小任务

1. 定义 `ContextPackage` 与各分区 token quota。
2. 将 tokenizer 计数放在唯一入口，不再同时使用字符估算和 token 计数。
3. Evidence 采用来源/模态配额，防止某一文档或纯文本占满上下文。
4. 高权重 anchor 先 pin，再去重、压缩、指针化。
5. 所有丢弃与压缩操作记录 lineage 和 reason code。
6. 用小型真实多 PDF 集重做压力实验，pseudo-source 只保留为单元压力测试。

### 验收

- 主链路每次 LLM call 均有 ContextPackage trace；
- 超预算请求不依赖先触发 context overflow 异常；
- 报告 token 节省率、gold retention、visual retention、answer/CoP 不退化；
- 有一个“节省 token 却丢失答案”的反例并修复。

## 9. S5：记忆控制

### 当前缺口

治理型 Memory Store 已有写入门控、TTL、supersede、workspace 隔离和审计测试，但真实聊天仍使用 `ConversationBufferWindowMemory` 和 session history file。

### 目标

把“聊天历史”与“可持久记忆”分开，只将允许的用户事实、偏好、确认决策和审核事件写入长期 Memory。

### 小任务

1. 定义 Session / User Fact / Decision / Review Memory 边界。
2. 将 write gate 接到真实回答后处理，默认不写入。
3. 把 read gate 接入 Context Engine，按 workspace、namespace、TTL、topic 和 ACL 过滤。
4. 明确冲突记忆是 supersede、review 还是拒绝。
5. 证据不写入 Memory，只保留 evidence pointer。
6. 增加跨会话、跨 workspace、过期、冲突和删除的真实 API 测试。

### 验收

- 真实主链路不再直接将所有对话当长期记忆；
- 记忆读写均有 provenance 和 audit event；
- 报告 write precision、memory recall、stale hit、cross-session contamination 和 ACL leakage；
- 串味率与泄漏数必须为 0，否则不进入 S6。

## 10. S6：引用与回答控制

### 当前缺口

Claim Map、Verifier 和 Review Queue 已有实现，但未成为真实回答的必经环节。现有 B3 实验通过更严格的拒答/升级降低了 unsupported rate，但 fact recall 仅为 0.1667。

### 目标

在回答输出前强制做 Claim-Evidence 绑定，并在证据不足时进入受控的重检索、修订、人工审核或拒答路径。

### 小任务

1. 从 draft answer 拆分 atomic claims、数字、单位和限定条件。
2. 每个 claim 绑定 Evidence ID、source span、page 和 scope。
3. Citation Grader 输出 supported / unsupported / conflict / out-of-scope。
4. 建立最大一次的 retrieve-revise 环，避免无界自我反思。
5. 低置信、冲突和高风险 claim 进 Review Queue。
6. 输出端显示答案、引用和可解释的 verification status。

### 验收

- 主链路不能绕过 verifier 直接返回最终答案；
- 每个可验证 claim 都有证据或明确的 unsupported 状态；
- 报告 citation precision/coverage、claim support、HR、CiH/source drift 和 fact recall；
- 同时设置安全性和可回答性门槛，不允许通过全部拒答刷高安全指标。

## 11. S7：视觉证据控制

### 当前缺口

有真实 PDF 视觉解析产物，但当前 ViR 主要由合成 fixture 计算；上游输出未稳定提供 bbox，尚无真正的 modality-aware rerank 和 visual fact gate。

### 目标

使视觉答案不只依赖 VLM 生成描述，而是能返回原 PDF 页、图号/表号、区域或最小可定位视觉证据。

### 小任务

1. 先冻结一个 5–10 题的真实视觉小集，人工标注 page/figure/table/gold fact。
2. 补全 visual evidence 的 page、figure ID、caption、neighbor text 和 artifact pointer。
3. 若上游没有 bbox，先明确使用 page/figure-level evidence，不伪造 region-level 能力。
4. 建立 text-to-visual 与 visual-to-text 召回，再做 modality quota 和 rerank。
5. Visual Fact Gate 检查回答是否真的来自视觉证据，而不是文本旁证。
6. 比较文本单路、视觉单路和多模态融合。

### 验收

- 至少一个真实 PDF 视觉问题经过主 Agent 链返回可定位证据；
- 报告 Visual Recall@K、ViR、视觉错引率和延迟；
- 真实指标与 synthetic fixture 指标分开报告；
- 无 bbox 时简历只写 figure/page-level evidence，不写 region-level。

## 12. S8：评测、可观测与数据飞轮

### 当前缺口

Eval Harness 已有完整结构，但部分指标由 fixture 或离线模块给出，不一定来自真实主链。最新的部分 M9 报告因缺少多层候选指标，当前无法通过 Release Gate。

### 目标

把每次真实 Agent run 变成可回放、可比较、可归因、可进入 bad-case 修复循环的数据。

### 小任务

1. 为真实主链统一 run_id、trace_id、config/data/model fingerprint。
2. 确定性指标自动计算；LLM Judge 指标明确模型、prompt 和版本。
3. 对 Judge 结果做小比例人工抽检，记录 agreement；无人工抽检时不声称客观满分。
4. 任意失败 case 都可按原 config/data 重放。
5. Bad Case Registry 记录缺陷类型、归因阶段、修复 commit 和回归 case。
6. Release Gate 同时检查 retrieval、unsupported rate、recovery、memory isolation、visual 和 judge/human agreement。

### 验收

- 一条命令运行全层候选评测，不允许只跑 control 就标记完成；
- 候选报告通过 Release Gate，或明确呈现未通过的回归；
- 至少一个真实 bad case 完成“发现→归因→修复→回归”；
- 简历数字都能回到 report 和 case。

## 13. S9：端到端收口

### 目标

停止维护“原聊天链 + 新控制链”两套并行系统，形成一条用户可演示、面试时可解释的主链。

### 小任务

1. `/query` 与 `/stream-query` 统一进入控制面，`/runs` 作为任务资源 API。
2. 旧链保留短期 feature flag 回退，通过验收后删除。
3. 提供一键 smoke，覆盖 ingest、query、retrieve、verify、memory、resume 和 trace。
4. 修复启动竞态、废弃 API、core dump 根因与日志轮转。
5. 编写架构图、演示脚本、限制清单和面试 Q&A。

### 最终验收

- 真实查询的 trace 同时包含 Router、Plan、Policy、Tool、Retrieval、Context、Verifier、Memory 和最终答案；
- 中断后可恢复，重复执行无重复副作用；
- 真实文本与视觉 case 均有可追溯 Evidence；
- 全量测试和 Release Gate 通过；
- 服务重启后仍能查询历史 run、review 和 trace；
- 用户能在 5 分钟内自己讲清“为什么改、怎么改、指标怎么得到、哪些还没做”。

## 14. 每个小任务给 Kimi Code 的固定模板

```text
你现在只执行 RADIANT-Control 的【阶段/小任务名】，不得开始后续阶段。

目标：
<一句话的可验证目标>

修改前先完成：
1. 列出相关文件和当前调用链。
2. 解释关键类/函数的输入、输出、状态和副作用。
3. 指出缺口在代码中的确切位置。
4. 提出最小修改方案，等待确认，不立即修改。

确认后才执行：
1. 先新增一个能暴露当前缺口的测试。
2. 只修改本任务需要的文件。
3. 运行定向测试和一个真实 smoke case。
4. 输出 git diff --stat 与关键 diff 说明。
5. 记录 before/after、失败 case、剩余限制和回滚方法。

禁止：
- 不得一次完成整个阶段或后续阶段。
- 不得删除失败 case 来使测试变绿。
- 不得把 mock/synthetic/proxy 结果写成真实生产指标。
- 不得改动用户未授权的文件。
- 不得自动 commit、push、删除数据库、日志、artifact 或 core dump。

停止条件：
<本小任务的明确验收条件>

完成后立即停止，等待人工审查。
```

## 15. 我们共同执行时的对话格式

每次只处理下列四种之一：

1. **理解**：“带我读 Sx 的当前代码，先不修改。”
2. **设计**：“给出 Sx-y 的最小设计和失败测试，先不修改。”
3. **实现**：“按已确认的 Sx-y 设计实现，完成后停止。”
4. **复盘**：“带我逐文件审查 Sx-y 的 diff、测试和 trace。”

不使用“按计划全部完成”这类指令。

## 16. 简历表达规则

只写已经通过当前阶段验收的事实：

- 模块存在但未接入主链：写“实现原型/建立评测骨架”；
- 已接入真实主链：写“重构/接管/构建端到端链路”；
- proxy/synthetic：必须在表达中标明，或者不作为最终成果数字；
- 只有有可重放 report 的指标才写百分比；
- 自研 StateGraph 不写成 LangGraph；
- 未接入真 Cross-Encoder 不写 Cross-Encoder rerank；
- 无 bbox 不写 region-level visual evidence；
- 上游许可和代码归属只按可验证事实陈述。

## 17. 第一次共同执行的边界

第一次只做 **S0-1：仓库状态与两条主链路解读**：

1. 重新连接远程仓库；
2. 读取 API 入口、`convchain_api`、`RunRuntime`、Router、Planner、Registry 和 DurableRunner；
3. 画出旧链与新链的类/函数级调用图；
4. 解释每个组件的输入、输出、状态和副作用；
5. 不修改代码，不清理文件，不启动 S1。

完成 S0-1 后，我们先讨论：“让哪一类真实问题成为第一个被新控制面接管的 vertical slice？”
