# ADR 0003：步骤输出引用机制（Step Output Binding）

- 日期：2026-09-24
- 状态：已接受
- 关联：docs/CONTRACTS_SLICE1.md（§3 ExecutionPlan 目标形态）；V3 计划第 8 节"S1 步骤输出引用机制的冻结方向"；S1-7A / S1-7B / S1-7C 任务卡
- 前置阅读：ADR-0002（证据身份）、`app/control/models.py`（`PlanStep`）、`app/control/schema_guard.py`、`app/durable/runner.py`

## 背景

首个 vertical slice（只读 PDF 知识问答）要求 search→inspect 两步计划：第二步 `evidence.inspect` 的 `evidence_ids` 必须来自第一步 `evidence.search` 的真实输出，而不是 Planner 在计划时编造的 ID（Planner 计划时没有任何检索结果，编造即 FM-3"编造 ID"）。

现状的三个硬事实：

1. `ToolRegistry.invoke(tool, step.arguments, ...)` 原样传计划里的字面量参数（`app/durable/runner.py:513-514`、`app/control/registry.py:196`），步骤间没有任何数据通道；`depends_on` 只被 `_topological_order` / `StateGraph` 用来排序（`app/control/scheduler.py:36`、`app/durable/graph.py`），不传递数据。
2. `PlanStep.arguments` 是 `dict[str, Any]`（`app/control/models.py:154`），全部契约模型 `extra="forbid"`。
3. 步骤输出已经持久化：成功的 `ToolResult.output` 被 `_save_step` 写进 checkpoints 表（`app/durable/runner.py:452-460`、549-573），resume 时从同一 SQLite 文件恢复（runner.py:224-226）——绑定解析的数据源天然存在。

同时 V3 冻结方向禁止字符串模板 eval，要求显式、可校验的结构化引用；纯字面量 arguments 必须兼容（既有单步 plan 与全部既有测试不得退化）。

## 决定

**引用是 `PlanStep.arguments` 的内嵌受控值类型，不引入独立 `bindings` 字段，禁止字符串模板。**

### 受控值类型

新增 pydantic 模型（落在 `app/control/models.py`）：

```python
class StepOutputReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: Literal["step_output"] = "step_output"  # 受控值类型判别标记
    from_step: str          # 语义对应冻结方向的 $from_step
    path: str               # 语义对应 $path；受限语法，见下
    expects: str            # 声明的目标 JSON 类型，如 "array<string>"
```

`PlanStep.arguments` 的值域变为"字面量 | StepOutputReference"。语义示例（V3 第 8 节）`{"$from_step": "s1-search", "$path": "output.hits[*].evidence_id"}` 映射为：

```json
{"evidence_ids": {"ref": "step_output", "from_step": "s1-search",
                  "path": "output.hits[*].evidence_id", "expects": "array<string>"}}
```

`path` 语法为受限子集：以 `output` 起始，随后只能是 `.key`（dict 键）与 `[*]`（列表展开），不允许索引数字、切片、函数调用或任意表达式。逐键求值；`[*]` 对其左侧列表逐元素应用后续段。

### Guard 的静态校验（S1-7A）

`SchemaGuard.validate` 在每个 step 过 schema 之前先展开引用，全部失败都是 typed reason code，任一存在则整个 plan 被拒（`GuardResult.ok=False`，工具调用次数为 0）：

1. `from_step` 必须存在于 `plan.steps`（step_id 集合），否则 `guard.binding_unknown_step:<step_id>-><from_step>`。
2. `from_step` 必须在当前 step 的**依赖闭包**内：沿 `depends_on` 传递可达。引用未来 step、引用并行无依赖 step 一律 `guard.binding_not_in_closure:<step_id>-><from_step>`。这同时复用 Guard 已验证的无环 DAG（schema_guard.py:137-138），闭包必为已完成步骤的子集。
3. `path` 必须能通过受限语法解析，否则 `guard.binding_invalid_path:<step_id>:<arg_key>`。
4. 静态类型相容性：`expects` 与目标参数在工具 `arguments_schema` 中声明的类型不可能匹配时（如 schema 要求 `integer` 而 `expects` 声明 `array<string>`），返回 `guard.binding_type_mismatch:<step_id>:<arg_key>`。path 键是否存在于输出中**不做**静态保证——输出形状由运行时决定，交给运行时错误。
5. 一个 arguments dict 中允许混用字面量与引用；纯字面量 plan 不经任何新逻辑（向后兼容的判定依据：值不是带 `ref: "step_output"` 判别标记的 dict）。

### Runner 的运行时解析（S1-7B）

在 `registry.invoke` **之前**、节点状态机进入 RUNNING 之后（`runner.py:_execute_step` 内、`_invoke_isolated` 调用前）解析：

1. 数据源是 `from_step` 的 **succeeded checkpoint output**（durable.db checkpoints 表）。执行中的内存 `results` dict 与 checkpoint 内容一致，但一律走 checkpoint/恢复路径，保证 resume、进程重启、fencing 接管后解析语义不变（不依赖仅存在于当前进程的临时变量）。
2. 解析产出 resolved arguments（全部为字面量），**重新过目标工具的 `arguments_schema`**（复用 `validate_arguments`，schema_guard.py:67-84）；不过即 typed error，工具调用次数为 0。
3. 空列表展开（`[*]` 产出 0 个元素且目标参数为必需）→ `runtime.binding_empty:<step_id>:<arg_key>`，该 step 标记失败、不调用工具；**下游 step 因闭包校验永远不会以空输入被调用**（planner 侧引用展开为空时，整条链 fail，不硬答——对应"sufficiency 不足时 abstain"的方向）。
4. `path` 中途键不存在 → `runtime.binding_path_missing:<step_id>:<arg_key>:<segment>`；类型不符（期望 string 拿到 object 等）→ `runtime.binding_type_mismatch:<step_id>:<arg_key>`。两者均为 TERMINAL_ERROR（非 transient，不重试）。
5. retry：解析在每次 attempt 前重做，输入是同一份 succeeded checkpoint，结果必然一致；checkpoint 中的源 step 输出不可变（supersede 产生新版本证据但不改写历史 run 的 checkpoint）。

### Trace 与敏感内容

- 计划本身（含引用模板）已持久化：`rt.plans`（内存，api.py:713）+ review 项 `plan_json`（review_queue.db）+ checkpoint 的 config_fingerprint 绑定。trace 无需重复抄录模板。
- 事件流（events 表，SSE 可读）在解析处新增一条 `binding_resolved` 事件（`app/durable/events.py` 新增 EventType），payload 只含：`step_id`、`arg_key`、`from_step`、`path`、`expects`、**解析出的元素数量**、元素 sha256 短摘要（canonical JSON 的前 16 位）、错误码。完整 resolved 参数不落事件流。
- 完整 resolved arguments 与工具输出一起只进 checkpoints 表（runner.py:452-460 既有行为）。证据正文本身不是 secret，但 resolved 值可能来自任意未来工具；按"trace 默认最小化"原则，事件流只记形状与摘要，原文一律查 checkpoint。禁止在事件 payload 中记录任何环境变量、连接串、密钥。

## 备选方案与取舍

- **独立 `bindings` 字段**（plan 级或 step 级另开一张映射表）：被否。一份计划出现两张参数表，Guard 要同步校验两处一致性（同键在 arguments 与 bindings 都出现时谁生效？）；planner 出错面翻倍；且 `PlanStep` 全模型 `extra="forbid"`，给 step 加字段等于改公共契约的可见面，而 arguments 内嵌受控值类型对纯字面量 plan 零影响。
- **arguments 内嵌受控值类型（本 ADR）**：Guard 单点校验（现有 `validate_arguments` 顺序不变，先展开引用再查 schema）；模板与数据同处一地，trace 只需一处来源；pydantic 判别标记让"这是引用"在反序列化时即可判定。代价：arguments 的值域变宽，Guard 必须第一个识别引用形状——已由判别标记 `ref` 解决，不接受任何无标记的 dict 被当作引用。
- **字符串模板**（`"{{s1-search.output.hits[*].evidence_id}}"`、f-string/eval）：被 V3 冻结方向明确排除。不可静态校验（step 存在性、依赖闭包、类型都要等运行时）；路径写错只能在执行中炸；模板引擎本身是注入面——而本项目 Router 专门设了 injection abstain（router.py:27-37），控制面不应再开第二个注入面。
- **共享内存 blackboard**（runner 维护一个跨 step 的可变 dict，step 互相读写）：被否。引入可变全局状态，resume 后 blackboard 为空必须重建，与"只从 checkpoint 恢复"的现有语义冲突；步骤间读写不受 Guard 约束，越权无法静态拦截。

## 影响

- **Guard**（`app/control/schema_guard.py`）：新增 4 个静态校验分支与对应 reason code；`_has_cycle` 先于闭包校验执行（现有顺序已满足）。失败语义不变：任一 code → `GuardResult.ok=False` → HTTP 422 / RunSummary REJECTED，0 次工具调用。
- **Runner**（`app/durable/runner.py`）：`_execute_step` 在 `_invoke_isolated` 前插入 resolve 步骤；成功路径 checkpoint 内容不变（resolved 值只作为 handler 入参，output 仍记工具真实输出）；失败路径产生 TERMINAL_ERROR，run FAILED（runner.py:366-387 既有语义）。
- **trace**：新增 `binding_resolved` EventType；NODE_STARTED payload 增加 `resolved_arg_keys: list[str]`（只列键名）。
- **checkpoint**：schema 不变——resolved 输入不额外落库（checkpoint 已存上游 output，足够重建）；fencing/config_fingerprint 语义不变（引用模板参与 plan 哈希，resolved 值不参与——指纹比对的是计划与注册表，不是运行时数据）。
- **Planner**（S1-7C）：只读 knowledge QA 必须产出含引用的两步 plan；`mock report body` 与 report/export 路径移出首条 slice。
- **测试**：`tests/control/` 加引用静态校验负向用例；`tests/durable/` 加进程内 vs resume 解析一致性用例（tests/api/conftest.py 已有 RunRuntime fixture 可复用）；既有 445 基线中的纯字面量 plan 全部不受影响。

## 失败语义表

| 场景 | 发现时机 | typed error（code 风格对齐现有 `guard.*`/`runtime.*`） | 工具调用 | run 结局 |
|---|---|---|---|---|
| `from_step` 不存在 | Guard 静态 | `guard.binding_unknown_step:<step>-><from>` | 0 次 | plan 被拒（422 / REJECTED） |
| 引用未来 step / 无依赖 step | Guard 静态 | `guard.binding_not_in_closure:<step>-><from>` | 0 次 | plan 被拒 |
| path 语法非法 | Guard 静态 | `guard.binding_invalid_path:<step>:<arg>` | 0 次 | plan 被拒 |
| `expects` 与目标 schema 类型不可能匹配 | Guard 静态 | `guard.binding_type_mismatch:<step>:<arg>` | 0 次 | plan 被拒 |
| path 中途键不存在 | 运行时解析 | `runtime.binding_path_missing:<step>:<arg>:<segment>` | 0 次 | step TERMINAL_ERROR → run FAILED |
| 展开类型不符 | 运行时解析 | `runtime.binding_type_mismatch:<step>:<arg>` | 0 次 | step TERMINAL_ERROR → run FAILED |
| `[*]` 展开为空（必需参数） | 运行时解析 | `runtime.binding_empty:<step>:<arg>` | 0 次 | step TERMINAL_ERROR → run FAILED，下游不被调用 |
| resolved 参数重过 schema 失败 | 运行时解析 | 复用现有 `guard.missing_argument` / `guard.argument_type_mismatch` | 0 次 | step TERMINAL_ERROR → run FAILED |
| 源 step 未成功（防御，Guard 已排除） | 运行时解析 | `runtime.binding_source_not_succeeded:<step>:<from>` | 0 次 | step TERMINAL_ERROR → run FAILED |

resume/retry 一致性：以上运行时错误每次 attempt 重新解析都会得到相同结果（数据源是不可变 checkpoint）；resume 后同样从 checkpoint 解析，不存在"内存里有、库里没有"的第二语义。
