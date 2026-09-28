# RADIANT-Control 双轨自动执行计划 V3

> 版本：V3（2026-09-23）  
> 实施对象：现有 `RADIANT-LLM` 仓库。不是新建 Visual-Parser 项目，也不是脱离 RADIANT-LLM 重写一套系统。  
> 核心目标：把当前并列的旧问答链、控制链、Evidence 摄取链，逐步收敛为一条可计划、可校验、可恢复、可评测、可解释的真实 Agentic Workflow。  
> 执行原则：**理解产物与代码完全分开，执行过程自动连续，关键风险才停。**  
> 本文是唯一执行计划。V2 仅作历史参考，不再驱动后续实现。

---

## 0. 给执行 AI 的一句话任务

严格按本计划从 `RADIANT-Control_CURRENT_TASK.md` 指向的位置继续：每个控制面先完成 **T1 理解轨**，将讲义写入 `docs/learning/`；T1 自检通过后无需等待用户，自动进入同阶段 **T2 编码轨**；T2 通过阶段门后自动进入下一阶段 T1。不得重复已验收工作，不得用 mock、硬编码答案或降低验收标准制造“完成”。

---

## 1. 为什么要采用双轨，而不是边讲边写

过去的 T1–T4 把讲解、设计、实现、复盘拆成多轮对话，存在三个问题：

1. 用户需要频繁等待，模型额度和上下文被重复说明消耗；
2. 讲解与终端日志混在一起，用户仍然不知道“系统原来怎样、现在怎样”；
3. 实现可能已经偏离讲解，但没有基于真实 diff 的独立证据。

V3 将每个控制面固定为两条相互隔离的轨道：

```text
Sx-T1 理解轨（只读代码，写中文讲义）
             │ 自动完整性检查通过
             ▼
Sx-T2 编码轨（测试→实现→smoke→回归→证据）
             │ 阶段门通过
             ▼
S(x+1)-T1
```

T1 是必须落盘的学习产物，不是人工等待点。T2 是自动实现闭环，不承担长篇教学。用户可在执行期间或执行结束后按顺序阅读 T1。

---

## 2. 唯一可信信息源与文件职责

| 文件/目录 | 职责 | 写入规则 |
|---|---|---|
| `RADIANT-Control_双轨自动执行计划_V3.md` | 稳定的总任务书 | 执行 AI 不得自行重写范围、降低阶段门 |
| `RADIANT-Control_CURRENT_TASK.md` | 当前游标、阻塞、最近证据、下一动作 | 每个子任务完成或失败后原子更新 |
| `RADIANT-Control_执行跟踪与数据飞轮.md` | 历史台账、指标、bad case、release gate | 遵循原文件 append-only 规则 |
| `docs/learning/Sx_<控制面>_理解.md` | 给用户看的独立中文讲义 | 仅 T1 写；T2 不得把终端日志塞入讲义 |
| `docs/adr/` | 关键架构决策 | 存在多种显著影响后续的方案时先写 ADR |
| `artifacts/control-v3/` | smoke、trace、测试、配置指纹等证据 | 不得用口头“已通过”代替产物 |
| `benchmarks/` | 冻结用例和 gold | 失败时不得改 gold 或删 case 让测试变绿 |

执行时的可信度优先级：

```text
真实代码/数据库/测试输出/trace
    > CURRENT_TASK
    > 执行跟踪台账
    > V2 与旧报告中的自报状态
```

若文档与代码矛盾，以代码和可复现实验为准，并在台账追加更正。

---

## 3. 当前真实起点（2026-09-23 审计）

### 3.1 Git 与测试

- 分支：`master`，相对 `origin/master` ahead 6；工作区存在用户已有修改和运行产物，禁止清理或覆盖。
- 最新提交：`994db75 fix: 模型就绪竞态三重修复`。
- 重新执行 `runtime/bin/python -m pytest -q`：**445 passed, 1 skipped, 22 warnings，182.66s**。
- 该数字只表示当前测试基线，不表示 S1–S9 的真实 Agent 主链已经全部完成。

### 3.2 已完成且不应重复

- S0-1～S0-7：代码地图、三条链、基线、风险台账、固定教学 case。
- S1-1：`QueryRequest` 与首条 vertical slice 契约。
- S1-2：确认最小检索包装点为 `direct_jsonl_kb_search`。
- S1-3：真实只读 `evidence.search` adapter。
- S1-4：真实批量 `evidence.inspect` adapter，并修复 `get_evidence` 的有效期列读取。
- S1-5 的代码主体：生产 `RunRuntime` 使用 `build_default_registry(evidence="real")`，相关定向测试通过。

### 3.3 当前不能误报为完成的缺口

1. `evidence.search` 依赖 `RADIANT_EVIDENCE_KB_DIR`；Evidence Store 依赖 `RADIANT_EVIDENCE_DB`。尚需证明真实服务启动配置中二者存在且正确。
2. 尚需从真实 HTTP `/runs` 路径完成一次无 feature flag、无 mock 的 search smoke，并保存 trace。
3. Router 的 intent/tool-call 规则主要为英文；中文固定问题可能只 `respond`，不创建 run。
4. `RulePlanner` 目前只生成 search，report 分支仍含 `mock report body`；尚未生成真实 search→inspect 计划。
5. `PlanStep.arguments` 是静态字典，Runner 直接把它传给工具；当前不存在“上一步输出绑定到下一步参数”的通用机制。
6. 契约文档中的 inspect 示例写死 gold Evidence ID，只能当 schema 样例，不能成为真实 Planner 实现。
7. 当前 search adapter 是 JSONL 关键词检索，不是 BM25+Dense+RRF；在 S3 之前禁止称为 Hybrid Retrieval。

### 3.4 当前执行位置

`S1-T1` 需要补齐剩余控制链讲义；`S1-T2` 从 **S1-5C 真实服务配置与 HTTP smoke** 继续。详细游标见 `RADIANT-Control_CURRENT_TASK.md`。

---

## 4. 全局禁止项

执行 AI 在任何阶段都不得：

- 新建一个脱离 RADIANT-LLM 的项目来替代本仓库；
- 把 Visual-Parser 当成主体，RADIANT-LLM 才是被改造对象；
- 写死服务器路径、项目根目录、PDF 目录或数据库绝对路径；
- 删除、覆盖或回滚用户已有 dirty files、DB、WAL、日志、backup、artifact、core dump；
- 自动 `git commit`、`git push`、`git reset`、`git checkout --`；
- 为让测试变绿而删除失败用例、改 gold、降低阈值或改成 skip；
- 把 mock/synthetic/fixture/proxy 指标写成真实系统成绩；
- 硬编码教学 case 的 Evidence ID、答案、页码或工具输出；
- 将自研 `StateGraph` 宣称为 LangGraph；
- 在没有真实模型 benchmark 前宣称 Cross-Encoder Rerank 已实现；
- 在没有真实 bbox 时宣称 region-level visual evidence；
- 未经来源核验就宣称当前整仓代码具有 Apache-2.0 许可；
- 为了“完整”引入核能谱图或额外领域数据库；本项目的数据基础是 RADIANT-LLM 已支持的核工程/技术 PDF 与解析产物。

---

## 5. 自动执行协议

### 5.1 启动顺序

每次新会话只需：

1. 读取本计划；
2. 读取 `RADIANT-Control_CURRENT_TASK.md`；
3. 读取执行跟踪文件当前阶段相关部分；
4. 检查 `git status --short --branch`；
5. 只读取当前阶段相关代码和测试；
6. 从游标后的第一项继续。

不得因为换模型或上下文丢失而从 S0 重做。

### 5.2 T1 理解轨合同

T1 只能读代码、测试、配置、trace 与历史产物；只能新建/更新对应的 `docs/learning/Sx_*.md`，不得修改业务代码。

每份 T1 讲义必须按以下顺序书写：

1. **200 字内人话摘要**：模块做什么、现在缺什么、修完用户能看到什么。
2. **修改前图**：不超过 12 个节点，边标出实际数据类型。
3. **修改后图**：明确新增控制点和失败出口。
4. **固定案例**：沿用 `TEACH-T01`，逐步展示输入和输出形状；不得写死 gold 作为运行参数。
5. **状态流**：哪些状态只在内存、哪些进入 SQLite、哪些进入 trace。
6. **3～5 个关键代码位置**：每个写输入、输出、副作用、失败行为、为什么重要。
7. **术语表**：最多 8 个术语，用人话解释。
8. **T2 变更清单**：预计修改文件、禁止修改文件、失败测试、smoke、回滚方式。
9. **三件必须记住的事**：必须正好 3 条。
10. **三道自测题与折叠答案**：数据流、设计取舍、失败后果各一道；不要求用户实时回复。
11. **面试表达**：一段 60 秒口述，以及“可以说/暂时不能说”。

T1 完整性检查：以上 11 项齐全、图与代码一致、没有把计划写成既成事实，才可自动进入 T2。

### 5.3 T2 编码轨合同

T2 不输出长篇教学，按下列循环自动执行当前阶段的子任务：

```text
确认允许范围
→ 跑相关 baseline
→ 写一个因目标缺口失败的测试
→ 确认失败原因正确
→ 做最小实现
→ 跑定向测试
→ 跑真实 smoke
→ 跑阶段回归
→ 审查 git diff 与运行产物
→ 更新台账和 CURRENT_TASK
```

每个子任务的最小记录：

- 任务 ID、开始/结束时间；
- 修改文件与新增文件；
- baseline 命令与结果；
- 失败测试及失败原因；
- 实现摘要（不超过 200 字）；
- 定向测试、smoke、回归结果；
- trace/artifact 路径；
- 新债务、限制与回滚方法；
- 是否通过子任务门。

### 5.4 自动继续规则

- T1 完整性通过：自动进入同阶段 T2，不等待用户。
- T2 子任务通过：自动执行下一子任务。
- 阶段门全部通过：自动创建下一阶段 T1 讲义并继续。
- 会话/额度即将结束：先更新 `CURRENT_TASK`，记录精确续跑命令，再自然停止。

### 5.5 必须暂停的情况

遇到以下任一情况，更新 `CURRENT_TASK` 为 `BLOCKED` 并停止：

1. baseline 在本任务修改前失败，且失败不属于当前目标；
2. 需要删除/覆盖用户数据或执行破坏性 Git 操作；
3. 需要新 API Key、付费服务、GPU、大规模数据或外部账号；
4. 实际修改必须越过本阶段边界；
5. 存在两个会显著影响后续契约或数据迁移的方案，计划未给出选择；
6. 同一测试经过三次有依据的修复仍无法通过；
7. smoke 没有经过目标模块；
8. 只能通过硬编码、mock、降阈值或改 gold 才能通过；
9. 发现敏感信息可能进入日志、trace、测试 fixture 或 Git diff。

普通实现细节、非破坏性重构方式和测试命名不是暂停理由，由执行 AI在计划边界内自行决定。

---

## 6. 全局固定案例与证据要求

### 6.1 教学案例

使用 `benchmarks/teaching_case.jsonl` 中 `TEACH-T01`：从已经摄取的 PDF 中回答一个技术概念，并返回 PDF 名、页码和 Evidence ID。

该案例用于解释和验收，不允许：

- 把 gold Evidence ID 写入 Planner；
- 把 gold 答案拼入工具 handler；
- 因当前 gold 已 supersede 就静默换答案；如需重冻必须保留版本和原因。

### 6.2 每个阶段的证据包

每个阶段在 `artifacts/control-v3/Sx/` 保存：

```text
baseline.txt
targeted-tests.txt
smoke-request.json
smoke-response.json
trace.jsonl（或 trace URI 索引）
regression.txt
diff-stat.txt
config-fingerprint.json
stage-report.md
```

秘密、token、密码和完整 `.env` 不得写入 artifact。

---

# 7. S0：基线与代码地图（已完成，只复用，不重做）

## S0-T1 理解轨

历史讲解已存在。后续 T1 只可引用，不得把个人笔记继续追加到总计划。

## S0-T2 编码轨

已完成基线冻结、风险登记与教学 case。除非后续阶段发现基线证据不可复现，否则禁止重做。

## S0 阶段门

- 三条链与断点有记录；
- 固定 case 已冻结；
- 全量测试可复现；
- 未删除未知运行产物。

状态：`DONE`。

---

# 8. S1：意图、计划、步骤绑定与真实工具控制

## S1 目标

让中文/英文 PDF 问题从 `/runs` 进入 Router，生成经过 Guard/Policy 校验的真实 search→inspect 计划；第二步从第一步真实输出取得 Evidence ID；生产 Registry 默认调用真实只读 adapter；全链无 `_MOCK_DOCS`、无硬编码 gold。

## S1-T1 理解轨

讲义路径：`docs/learning/S1_意图计划与工具控制_理解.md`

除第 5.2 节通用合同外，必须具体解释：

1. Router、Planner、Schema Guard、Policy、Registry、Adapter、Runner 的职责边界；
2. “识别用户意图”“选择工具”“授权工具”“执行工具”为什么是四件事；
3. 中文问题当前为什么落到 `respond`；
4. search adapter 如何从 JSONL hit 解析到真实 Evidence ID；
5. inspect adapter 如何做 workspace 隔离和有效期状态报告；
6. 为什么 `depends_on` 只决定顺序，不会自动传递数据；
7. 当前 Runner 直接传 `step.arguments` 的证据；
8. 步骤输出引用机制的设计、解析时机、失败语义和 trace；
9. 使用固定 case 展示 search 输出如何成为 inspect 输入；
10. 哪些指标只能在 S3/S8 完成后再写简历。

### S1 输出引用机制的冻结方向

不得采用字符串模板随意 `eval`。优先设计显式、可校验的结构化引用，例如：

```json
{
  "evidence_ids": {
    "$from_step": "s1-search",
    "$path": "output.hits[*].evidence_id"
  }
}
```

这只是语义示例，不是可直接复制的最终 schema。S1-T1 必须结合 Pydantic/Guard/Runner 现状完成 ADR，至少回答：

- 引用是 `PlanStep.arguments` 的受控值类型，还是独立 `bindings` 字段；
- Guard 如何校验被引用 step 存在且在依赖闭包中；
- path 不存在、类型不匹配、空列表时返回什么 typed error；
- retry/resume 时绑定读取 checkpoint 还是内存结果；
- trace 怎样同时记录模板参数与 resolved 参数且不泄露敏感内容。

如最终方案会改变公共契约，先写 `docs/adr/0003-step-output-binding.md`。ADR 是 T1 允许产物，不算业务代码。

## S1-T2 编码轨

按顺序执行，禁止跳项：

### S1-5C：真实服务配置与 HTTP smoke 收口

- 查明当前服务启动入口及配置加载方式；不得写死仓库路径。
- 在示例配置/启动说明中登记 `RADIANT_EVIDENCE_KB_DIR`、`RADIANT_EVIDENCE_DB`，不得提交真实秘密。
- 真实启动 API，不开启 `RADIANT_EVIDENCE_*_REAL` feature flag。
- 通过 HTTP 调用 `/runs` 或当前真实 run API，证明 Registry 为 real、`mock=false`。
- 保存请求、响应、trace、配置指纹；错误配置必须返回稳定 typed error，而不是静默退 mock。

验收：生产启动路径有明确配置；缺配置 fail closed；真实配置下 search 返回可查 Evidence ID。

### S1-6：中英文确定性 Router

- 冻结中英文 knowledge/visual/ingest/compare/audit、clarify、abstain 用例。
- 用参数化测试先证明当前中文固定 case 失败。
- 扩展确定性规则或轻量规范化；本阶段不引入 LLM Router。
- 保持 prompt injection 规则和低置信度出口。
- reason code 必须可解释，不能只返回一个标签。

验收：中英文等价问题决策一致；模糊请求 clarify；注入请求 abstain；固定 case 为 `tool_call`。

### S1-7A：步骤输出引用契约与 Guard

- 根据 ADR 新增受控引用数据结构；禁止任意表达式执行。
- Guard 校验来源 step、依赖关系、字段路径、目标类型的静态部分。
- 负向测试：未知 step、引用未来 step、非依赖 step、非法 path、类型不可能匹配。
- 原有纯字面量 arguments 必须兼容。

验收：非法引用在工具执行前 fail closed；旧单步 plan 不退化。

### S1-7B：Runner 参数解析、checkpoint 与 trace

- 在工具调用前解析引用；数据来源优先使用已成功 step 的 checkpoint/result。
- resolved arguments 必须重新通过目标工具 schema 校验。
- 空 hits、缺字段、类型不匹配产生稳定 typed error。
- retry/resume 后解析结果一致；不得依赖仅存在于当前进程的临时变量。
- trace 记录来源 step、path、解析数量、错误码；不得记录 secret。

验收：进程内运行与 resume 使用同一解析语义；空检索不会调用 inspect。

### S1-7C：真实 QA Planner

- 对只读 knowledge QA 生成 search→inspect 两步 plan。
- search 参数来自 QueryRequest/goal/workspace；inspect 的 Evidence IDs 来自结构化输出引用。
- 删除该路径中的 `mock report body`；report/export 不属于首条 slice。
- 预算和 timeout 使用现有配置入口，默认值可解释。

验收：plan JSON 中没有 gold ID、mock content 或绝对路径；依赖、风险和预算正确。

### S1-8：Guard/Policy 负向合同

至少覆盖：未知工具、错参数、额外参数、引用越权、风险降标、workspace 越权、超预算、写操作缺幂等键。断言工具 handler 调用次数为 0。

### S1-9：真实端到端 Tool Control smoke

固定 case 走：

```text
HTTP QueryRequest
→ RouterDecision(tool_call)
→ ExecutionPlan(search→inspect)
→ Guard/Policy
→ DurableRunner
→ real search
→ binding resolve
→ real inspect
→ checkpoint/event/trace
```

验收响应/trace 同时包含真实 PDF、page、Evidence ID、`mock=false`；无 `_MOCK_DOCS`；错误请求在工具调用前被拦截。

## S1 阶段门

- 全量测试不低于本计划记录的基线；新增测试全部通过；
- 中文和英文固定问题均创建真实 run；
- search→inspect 数据来自运行时绑定，不是硬编码；
- real handler 为生产默认，mock 仅用于显式测试 fixture；
- workspace 越权和非法引用 fail closed；
- trace 能解释 decision、plan、policy、工具输入输出来源；
- 用户讲义与实际实现一致。

## S1 简历事实门

通过后可以说：实现确定性意图路由、结构化计划、Schema/Policy 前置校验、真实工具 Registry 和步骤间数据绑定。  
尚不能说：Hybrid Retrieval、LangGraph、Cross-Encoder、Memory、多 Agent、完整回答生成链。

---

# 9. S2：Durable 执行控制

## S2 目标

把 S1 的真实两步 plan 放进可恢复执行系统，证明 retry、timeout、cancel、lease、fencing、幂等与 SSE 续传不是只有单测骨架，而是对真实工具链生效。

## S2-T1 理解轨

讲义路径：`docs/learning/S2_Durable执行控制_理解.md`

必须解释：run state/step state/checkpoint/event/log 的区别；当前自研 StateGraph；checkpoint 保存了什么；为何 retry 不等于 resume；lease 与 fencing 防什么；只读工具与有副作用工具的幂等差异；进程内 `rt.plans` 的风险；SSE `Last-Event-ID` 如何工作；固定 case 在 search 完成后崩溃怎样恢复 inspect。

必须画正常、超时、取消、崩溃恢复四张小图。讲义中明确：本项目当前不是 LangGraph，除非本阶段真实引入并验证。

## S2-T2 编码轨

### S2-1：状态机与非法转移测试

冻结真实两步 plan 的状态序列，补非法转移与并发边界测试。

### S2-2：持久化完整恢复输入

checkpoint/run store 持久化恢复所需的 plan、workspace、config fingerprint；重启后不依赖进程内 plan map。

### S2-3：真实 search/inspect 经过 Runner

checkpoint 保存真实 ToolResult 与引用解析所需输出；恢复时成功 step 不重跑。

### S2-4：Typed retry

只重试明确 retryable error；terminal/denied/binding error 不重试；记录 attempt 和分类。

### S2-5：Timeout 与 Cancel

timeout 后状态一致；cancel 后不启动新节点；后台线程晚返回不得覆盖终态。

### S2-6：Lease 与 Fencing

旧 owner 被接管后不能继续写 checkpoint/event，也不能触发下一个工具。

### S2-7：持久幂等

针对一个受控写操作验证重启+同 key 不重复副作用；只读搜索不得拿幂等指标冒充写操作证明。

### S2-8：SSE 断线续传

验证 `Last-Event-ID` 后 seq 连续，无丢失、无重复；客户端重复连接不启动第二个 run。

### S2-9：真实进程 kill/restart smoke

子进程中断后恢复固定 case，search 已成功则不重跑，inspect 继续；保存工具调用计数与事件序列。

## S2 阶段门

- 真实两步 run 可 cancel/resume；
- 恢复不依赖进程内 plan map；
- 成功 step 重复执行数 0；事件丢失/重复数 0；
- stale owner 写入数 0；
- 故障注入结果有 trace 和配置指纹。

## S2 架构决策

若考虑迁移 LangGraph，先写 ADR 比较：现有 durable 能力、迁移收益、checkpoint 兼容、测试成本、简历真实性。没有明确净收益则保留自研 StateGraph，并诚实命名。

---

# 10. S3：检索控制

## S3 目标

把当前 JSONL 关键词搜索升级为可解释、可评测的 BM25+Dense+RRF 检索控制链，并加入 metadata/version/workspace gate、相关性/充分性门和可选真实 reranker。

## S3-T1 理解轨

讲义路径：`docs/learning/S3_混合检索控制_理解.md`

必须解释当前 `direct_jsonl_kb_search` 与 Chroma 路径；Evidence ID 对齐问题；BM25 与 Dense 各自优势；用三条候选手算 RRF；gate 与 rerank 的顺序；anchor competition；Recall@K、MRR、nDCG、Anchor Hit、HR、CoP 分别回答什么；为什么单测通过率不是检索质量。

## S3-T2 编码轨

1. S3-1 冻结当前检索 baseline 和失败 case，不先改算法。
2. S3-2 统一 BM25/Dense/Evidence Store 的 chunk/Evidence ID 映射。
3. S3-3 接入 BM25 与 Dense，RRF 合并，trace 保存各路排名和最终排名。
4. S3-4 在 prompt 前执行 workspace/ACL/version/active metadata gate，记录 drop reason。
5. S3-5 加相关性、anchor、sufficiency gate；不足时 retrieve_more/abstain，不硬答。
6. S3-6 重现高竞争/多来源失稳 case，输出候选逐层变化。
7. S3-7 在同数据、同 seed、同 top-k 下跑关键词/Dense/Hybrid/Gated 对照。
8. S3-8 先 benchmark 再决定是否引入真实 Cross-Encoder；无净收益则写“不采用”的 ADR。

## S3 阶段门

- 真实 Agent search 使用 Hybrid Retrieval；
- 每条候选可追溯到各路排名、RRF、gate reason；
- 报告 Recall@K/MRR/nDCG/Anchor Hit、p50/p95 和多 seed 方差；
- 真实数据、synthetic fixture、proxy score 分开；
- 未加载真实模型时不出现 Cross-Encoder 简历表述。

---

# 11. S4：上下文预算与 Evidence 组装控制

## S4 目标

在 LLM 调用前主动构造有预算、有来源/模态配额、有 lineage 的 ContextPackage，避免“材料越多反而崩溃”的 anchor competition。

## S4-T1 理解轨

讲义路径：`docs/learning/S4_上下文预算控制_理解.md`

必须解释旧链字符估算、溢出后压缩与预算前置的差异；ContextPackage 分区；token budget；source/modality quota；anchor pinning；dedup；neighbor expansion；递归压缩；artifact pointer；token 节省率为何不等于质量提升。

## S4-T2 编码轨

1. 定义 ContextPackage/ContextItem 契约和唯一 token counter 入口。
2. 对 system、turn、memory、evidence、artifact、tool result 分区预算。
3. 加 source/modality quota、anchor pinning、去重、neighbor expansion。
4. 递归压缩但保留原 Evidence ID、source span 和 artifact pointer。
5. 接入真实回答 LLM 前；没有预算 trace 时不得调用模型。
6. 跑真实小型多 PDF 与单独标记的 pseudo-source 压力对照。

## S4 阶段门

- 每次模型调用前都有 ContextPackage trace；
- 超预算不会等报错后才补救；
- gold/visual evidence 的保留和丢弃原因可解释；
- 报告 token ratio、gold retention、visual retention、答案质量；
- 崩溃区间不比 baseline 退化。

---

# 12. S5：Memory 写入、召回与隔离控制

## S5 目标

把“聊天历史文件”升级为可治理 Memory：明确会话、事实、证据边界；默认拒绝不可靠写入；支持 namespace、ACL、TTL、supersede、冲突审核和可删除性。

## S5-T1 理解轨

讲义路径：`docs/learning/S5_Memory治理控制_理解.md`

必须用 10 条样例区分 chat history/session summary/long-term memory/evidence；解释为什么 evidence 不能复制为用户事实；Write Gate、Read Gate、namespace、TTL、supersede、conflict、provenance；展示跨会话串味和跨 workspace 泄漏路径。

## S5-T2 编码轨

1. 冻结 MemoryRecord、namespace、生命周期与 provenance 契约。
2. 回答后接 Write Gate，默认 deny；确认偏好/决策才写，模型猜测进入 review/deny。
3. Context Engine 前接 Read Gate，执行 workspace/namespace/TTL/ACL/topic 过滤。
4. 实现 supersede 和 conflict review，禁止静默覆盖。
5. 实现软失效/真删除边界和审计事件。
6. 真实 API 跨会话、跨 workspace 测试。
7. 跑 write precision、Recall@K、stale hit、pollution、contamination、leakage 指标。

## S5 阶段门

- 长期 Memory 不等于所有聊天落盘；
- 串味率 0，ACL 泄漏数 0；
- stale/superseded 记录不进入上下文；
- 每次写入、拒绝、覆盖、删除均可审计。

---

# 13. S6：Claim、引用、受控角色协作与人工审核

## S6 目标

把“一次生成直接返回”改为有界的 Draft→Claim→Evidence→Verify→Revise/Review 工作流。角色可拆分，但必须由同一状态机、预算、Policy 和 trace 控制，不能变成多个 Agent 自由聊天。

## S6-T1 理解轨

讲义路径：`docs/learning/S6_Claim引用与受控协作_理解.md`

必须解释 citation 与 claim support 的区别；atomic claim；数字/单位/限定词；Claim-Evidence Map；supported/unsupported/conflict/out-of-scope；有界反思；何时重检索、修订、拒答、入人工审核；受控角色协作与“多 Agent 自由对话”的区别。

## S6-T2 编码轨

1. 确定性优先地抽取 atomic claims，输出 claim ID/type/numeric fields。
2. 建 Claim-Evidence Map，强制 Evidence ID/source/page/span。
3. 实现 Citation/Scope Grader 的结构化 verdict 与 reason。
4. 接入最多一次 retrieve→revise 环，受 token/tool/wall-time 预算约束。
5. 将 Retriever/Writer/Verifier 作为受控角色节点；共享结构化状态，不共享任意长对话。
6. conflict/high-risk 进入 Review Queue，approve/edit/reject 后能 resume 原 run。
7. 最终 API 返回答案、引用和每条 claim 的验证状态。
8. 联合评测 citation precision/coverage、HR、CiH/source drift、fact recall，禁止全拒答刷安全分。

## S6 阶段门

- 真实回答无法绕过 Verifier；
- 每条可验证 claim 有证据或明确 no-evidence；
- 反思/修订最多一次且预算可见；
- Review Queue 携带真实 claim/evidence/plan；
- 角色节点所有动作都进入统一 trace。

---

# 14. S7：真实视觉证据控制

## S7 目标

让图、表、公式等视觉解析产物成为可定位、可召回、可校验的 Evidence，而不是把 VLM 生成的描述直接当作事实。

## S7-T1 理解轨

讲义路径：`docs/learning/S7_视觉证据控制_理解.md`

必须检查一份真实 parser artifact，区分 page、figure/table、caption、description、bbox、artifact pointer 哪些真的存在；解释 text-to-visual、visual-to-text neighbor、modality quota、Visual Fact Gate、ViR，以及“VLM 描述不等于视觉事实证明”。

## S7-T2 编码轨

1. 人工冻结 5～10 个真实 visual gold case，synthetic 分开。
2. 补全视觉 Evidence schema；无 bbox 就诚实使用 page/figure-level。
3. 实现 text-to-visual recall 和 visual-to-text neighbor expansion。
4. 实现 modality quota/融合排名，避免文本挤掉全部视觉证据。
5. 接 Visual Fact Gate，只有真实视觉 evidence 才计 visual support。
6. 至少一个真实视觉问题经过 Router→Planner→Runner→Retrieval→Context→Verifier。
7. 对比文本单路、视觉单路、融合：Visual Recall@K、ViR、错引率、延迟。

## S7 阶段门

- 可定位到 PDF/page/figure/table/artifact；
- 没有虚构 bbox；
- 真实 ViR 与 fixture 指标分开；
- 至少一个真实视觉 case 端到端通过。

---

# 15. S8：Eval Harness、可观测、Release Gate 与数据飞轮

## S8 目标

把各控制面变成一键可回归的 Harness，并让任何简历数字都能追到 case、run、trace、配置和 commit。

## S8-T1 理解轨

讲义路径：`docs/learning/S8_评测可观测与数据飞轮_理解.md`

必须解释 deterministic metric、LLM judge、人工抽检、operational metric；run/trace/config/data/model fingerprint；offline module test 与真实 Agent E2E 的区别；bad case 从发现到回归的生命周期；Release Gate 为什么不能只检查“测试绿”。

## S8-T2 编码轨

1. 统一 run/trace/config/data/model fingerprint，禁止记录 secret。
2. Eval Adapter 调真实 Agent 主链，不只调用离线模块。
3. 注册分层指标；未测值写 `not_measured`，不得补 0 或假分。
4. 加小比例 LLM Judge 与人工抽检，记录 judge prompt/model/version/agreement。
5. 建可审计 Bad Case Registry：失败阶段、证据、修复 commit、回归 case。
6. Release Gate 覆盖 control/runtime/retrieval/context/memory/claim/visual/ops 必需层。
7. 至少完成一个“发现→归因→修复→冻结回归→gate”真实闭环。
8. Dashboard 只读取统一指标与 trace API，不复制一套计算逻辑。

## S8 阶段门

- 一条命令运行完整候选 Harness；
- 报告链接真实 Agent run/trace；
- 一项指标能从简历数字追到原始 case；
- 至少一个 bad case 完成数据飞轮闭环；
- Release Gate 要么通过，要么诚实阻断发布。

---

# 16. S9：统一主链、部署治理与求职交付

## S9 目标

收敛 `/query`、`/stream-query`、`/runs` 的语义和默认路径，治理运行产物，完成可演示、可复现、可面试的真实项目交付。

## S9-T1 理解轨

讲义路径：`docs/learning/S9_统一主链与求职交付_理解.md`

必须解释旧链与新链如何迁移；feature flag/降级策略；配置、DB、WAL、日志、artifact、core、backup 的边界；健康检查与恢复；完整请求流/数据流/状态流；每个简历指标的数据来源和限制。

## S9-T2 编码轨

1. 统一三个 API 的语义，默认走新控制链；旧链保留显式迁移开关。
2. 新链失败不得静默切旧链；降级必须返回 reason 和 trace。
3. 整理 `.gitignore`、运行目录、数据库/WAL/artifact 管理，禁止直接删除用户数据。
4. 定位 1.4GB core dump 与启动竞态根因，先复现/诊断再修；不得只删 core。
5. 配置日志轮转、健康检查和服务重启后的历史状态读取。
6. 一键小数据 E2E smoke 覆盖 query/retrieve/context/verify/memory/resume/trace；ingest 只在有必要时单独验证。
7. 更新与真实代码一致的架构图和 5 分钟 demo；图上每个关键模块可在 trace 中找到。
8. 生成简历 4～5 行、面试 Q&A、限制清单，只使用已验收事实。

## S9 最终门

- 默认只有一条真实受控 Agent 主链；
- 固定文本 case 与至少一个视觉 case 通过；
- 中断可恢复，重放无重复副作用；
- Harness 与 Release Gate 通过；
- 一键 smoke 可在当前云环境复现；
- 简历数字均有 artifact、配置指纹和测试日期；
- 用户能完成 5 分钟介绍和 10 分钟架构追问。

---

## 17. 阶段报告模板

每个 Sx-T2 完成后写 `artifacts/control-v3/Sx/stage-report.md`：

```markdown
# Sx 阶段报告

## 1. 目标与范围
## 2. 修改文件
## 3. 新增失败测试及原始失败
## 4. 最小实现
## 5. 定向测试
## 6. 真实 smoke 与 trace
## 7. 阶段回归
## 8. 指标与配置指纹
## 9. 未完成限制/债务
## 10. 回滚方法
## 11. 与 T1 讲义一致性检查
## 12. 阶段门逐项结论
```

不得只写“全部通过”；每项至少给命令、结果和 artifact 路径。

---

## 18. CURRENT_TASK 更新格式

每完成/失败一个子任务，都用以下结构覆盖 `RADIANT-Control_CURRENT_TASK.md`：

```markdown
# RADIANT-Control Current Task

- updated_at:
- plan_version: V3
- stage:
- track: T1 | T2
- task_id:
- status: READY | RUNNING | BLOCKED | DONE
- last_verified_commit:
- dirty_worktree_acknowledged: true

## 已验证完成
## 本次实际修改
## 测试与 artifact
## 当前阻塞
## 下一条唯一动作
## 续跑命令
## 禁止重复/禁止进入
```

`下一条唯一动作` 只能有一项，避免换模型后自行重规划。

---

## 19. 执行 AI 的首次启动提示词

```text
你是 RADIANT-Control 的执行者。仓库是现有 RADIANT-LLM，不得另建项目。

先读取：
1. RADIANT-Control_双轨自动执行计划_V3.md
2. RADIANT-Control_CURRENT_TASK.md
3. RADIANT-Control_执行跟踪与数据飞轮.md 中当前阶段

然后检查 git status、相关代码和测试，从 CURRENT_TASK 指向的位置继续。

严格执行双轨：
- 每个 S 阶段先完成 T1 中文理解讲义，T1 只读代码，不改业务代码；
- T1 完整性自检通过后不等待用户，自动进入同阶段 T2；
- T2 只做失败测试、最小实现、真实 smoke、回归和证据记录；
- 阶段门通过后自动进入下一阶段 T1；
- 每个子任务后立即更新 CURRENT_TASK；
- 不重复已验收任务，不自动 commit/push，不删除任何用户文件；
- 不得硬编码 gold、使用 mock 冒充真实结果或降低验收标准；
- 遇到计划第 5.5 节停止条件立即记录证据并停止。

从当前游标开始执行。
```

---

## 20. 中断后的续跑提示词

```text
继续 RADIANT-Control V3 双轨计划。

先读 RADIANT-Control_CURRENT_TASK.md，再只读取 V3 当前阶段和执行台账相关部分。
检查 git status 和现有测试，从“下一条唯一动作”继续。
不要重复 DONE 项，不要绕过 BLOCKED 项，不要重写总计划。
完成子任务后先更新 CURRENT_TASK，再继续自动执行。
```

---

## 21. 最终简历表达约束

简历只允许使用已通过阶段门且有 artifact 的事实。表达应遵循：

```text
缺陷：RADIANT-LLM 原链具体失控在哪里
行动：增加了哪个控制面与工程机制
验证：用什么冻结 case、指标、故障注入或 trace 证明
结果：真实数字、适用范围与限制
```

不得写：

- “基于 Apache-2.0 二开”——除非已核验实际取得代码的来源和许可链；
- “LangGraph 重构”——除非代码真实引入并作为主链；
- “Cross-Encoder Rerank”——除非加载真实模型并完成对照；
- “100% 准确”——除非定义、数据、样本量和 artifact 全部可追溯；
- 论文中的 CoP/CiP/CiH/HR/ViR 数字作为本项目成绩。

最终理想项目主线是：

```text
Intent/Plan Control
→ Schema/Policy Guard
→ Durable Tool Execution
→ Hybrid Retrieval
→ Context Budget
→ Governed Memory
→ Claim/Citation Verification
→ Visual Evidence
→ Eval/Observability/Data Flywheel
```

它首先是一个“可控的核工程 PDF Agent”，而不是一个只强调解析内容的文献助手。
