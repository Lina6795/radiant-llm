# Memory Governance（RADIANT-Control M6）

- 日期：2026-09-22
- 状态：M6 实现完成（验收门见文末）
- 实现：`app/memory/`（`models.py` / `write_gate.py` / `read_gate.py` / `supersede.py` / `store.py` / `metrics.py`）
- 测试：`tests/memory/`（68 例全绿）、冻结用例 `benchmarks/memory_cases.jsonl`（17 条）

本文定义长期记忆的分类、Write/Read Gate 规则、supersede/TTL 语义、与现有
`session_store` 的边界，以及六项治理指标的口径。

## 0. 定位与边界

上游现状（`app/radiant_llm.py:1959` 附近）把会话 JSONL 直接 TF-IDF/fuzzy 注入
上下文，等于"聊天记录冒充长期记忆"。本库是独立的治理层：**模型只能提
`MemoryCandidate`，任何写入必须过 Write Gate，任何召回必须过 Read Gate**。
本 milestone 不修改 `radiant_llm.py` / `app/utils/session_store.py`，接线由主控
后续统一进行。

与 `app/utils/session_store.py` 的边界：

- `session_store` 的会话 JSONL 属于 **session 类**记忆（当前任务短期状态/摘要）。
  本库不改写它，只定义接口兼容：session 类 `MemoryCandidate` 不要求用户确认、
  走同一个 provenance/TTL 语义，未来接线时会话摘要可作为 session 类候选写入。
- 从会话内容"提升"为 user_fact/decision 必须重新走 Write Gate（含人工确认），
  禁止自动提升。

## 1. 分类定义

| 类别 | 含义 | 写入要求 |
|---|---|---|
| `session` | 当前任务短期状态/摘要 | provenance + write_reason + 置信度阈值 |
| `user_fact` | 用户明确确认的稳定事实 | 另需显式用户确认标记 + confirmation id；**禁止**由外部 evidence 内容转换 |
| `decision` | 用户/Reviewer 确认的项目决策 | 同 user_fact 的确认要求 |
| `evidence_pointer` | 指向 `app/evidence` 的 evidence_id | 必须携带 evidence_id（只存指针，不存证据内容） |
| `artifact_pointer` | 文件/图片/报告指针 + 元数据 | provenance + write_reason |

每条长期 Memory（`MemoryRecord`）字段：`memory_id, category, namespace,
workspace, subject, value, provenance{origin, source_run_id,
source_session_id, user_confirmation_id, evidence_id}, confidence,
sensitivity, write_reason, created_at, valid_from, valid_to, ttl_seconds,
superseded_by, status`。**无 provenance 来源或 write_reason 的候选一律被
Write Gate 拒绝**，因此库内无来源记录比例为 0（构造保证）。

## 2. Write Gate 规则（fail-closed，全部 reason 机器可读）

按 `app/memory/write_gate.py` 实现，拒绝原因全集：

| reason | 规则 |
|---|---|
| `missing_provenance` | run/session/confirmation 三类来源全空 |
| `missing_write_reason` | 写入理由为空（pydantic 层已校验，双保险） |
| `readonly_namespace` | `skills` / `skill_library` / `policy` 命名空间只读，任何写入拒绝 |
| `evidence_as_user_fact` | user_fact 携带 evidence_id → 一律拒绝（外部证据只允许指针形式） |
| `evidence_pointer_missing_target` | evidence_pointer 无 evidence_id |
| `low_confidence` | 低于阈值（默认 0.5，可配） |
| `secret_detected` | 命中密钥/凭证模式（sk-*、AKIA*、私钥头、api_key=、Bearer 等），**确认也不许存** |
| `personal_requires_confirmation` | 命中个人信息模式（手机号/身份证/邮箱）且未确认 |
| `prompt_injection` | 未确认候选命中注入模式（"记住：…""写入长期记忆""system: …""ignore previous instructions"等中英变体） |
| `missing_user_confirmation` | user_fact/decision 缺显式确认标记或 confirmation id |
| `duplicate_value` | 同 workspace+category+subject 已有同值活跃记录 |

冲突（同 subject 不同值）**不覆盖**：已确认 → `CONFLICT_REVIEW`（人工 review
或显式 supersede）；未确认 → `CONFLICT_CLARIFY`（向用户澄清）。旧值保持不动。

## 3. Read Gate 规则

- **workspace 严格隔离**：只查 `workspace =` 当前工作区，跨 workspace 泄漏数
  必须为 0；相似主题（甚至同名实体）也不例外。
- **过期不召回**：TTL（`created_at + ttl_seconds <= now`）或 `valid_to` 已过的
  记录默认不召回；superseded 记录同样不召回。两者仍可经 store 直查/审计轨查询。
- **任务相关性**：query 带文本时按 token 重叠打分（拉丁词 + CJK unigram/bigram，
  subject 权重 2×value，`score = (2·|Q∩S| + |Q∩V|) / (2·|Q|)`，阈值默认 0.1），
  过滤同 workspace 跨会话的相似主题串味；`top_k` 截断。
- **只读命名空间可读**：`skills`/`policy` 写入拒绝但读取允许（policy context）。

## 4. supersede / TTL / 删除语义

- **supersede 不覆盖历史**（`app/memory/supersede.py`）：新候选仍须过 Write Gate
  （跳过冲突检查，因替换是显式的），且必须同 workspace+subject；旧记录置
  `status=superseded, superseded_by=<新 id>`，值原样保留，默认不召回、可审计。
- **TTL 清理**（`store.cleanup_expired`）：只真删**过期的 active** 记录，逐条写
  `ttl_cleanup` 审计；superseded 历史不动。
- **显式删除**（`store.delete`）：真删行 + `delete` 审计（含完整快照）。
- 审计轨 `memory_audit` 追加写：action ∈ {write, supersede, delete, ttl_cleanup}，
  含 actor、时间、记录快照。

## 5. 指标口径（`app/memory/metrics.py`，纯函数、全离线）

| 指标 | 定义 | 验收 |
|---|---|---|
| write precision | gate 放行的写入中合法比例 = legit∧allowed / allowed | = 1.0 |
| memory Recall@K | top-K 召回中命中的相关记忆 / 相关记忆总数 | ≥ 0.8 |
| stale hit rate | 召回结果中已过期记录比例 | = 0 |
| conflict detection recall | 真冲突中产出 conflict 决策的比例 | = 1.0 |
| 跨 workspace 泄漏数 | 召回结果中 workspace 不匹配的记录数 | = 0 |
| 无 provenance 记录比例 | 库内缺来源或 write_reason 的记录比例 | = 0 |

实测（`tests/memory/test_metrics.py`，混合 3 合法 + 8 攻击/非法写入套件）：
write precision 1.0、Recall@5 1.0、stale hit 0、conflict recall 1.0、泄漏 0、
无来源比例 0。

## 6. 验收门对照

| 门 | 结果 |
|---|---|
| 冻结隔离：两 workspace 同名实体跨域泄漏 = 0 | 通过（`test_workspace_isolation.py` + MEM-14） |
| 每条 Memory 有 provenance + write_reason，否则拒写 | 通过（gate 规则 1/2 + 库内比例 0） |
| supersede 可审计、旧值不默认召回 | 通过（`test_supersede.py` + MEM-15） |
| 显式删除与 TTL 清理有测试（真删 + 审计） | 通过（`test_ttl_and_delete.py` + MEM-13/16） |
| 注入拦截（中英变体） | 通过（11 条变体参数化 + MEM-05/06） |
| evidence → user_fact 拒绝 | 通过（`test_write_gate.py` + MEM-04） |
| 冻结用例 ≥ 12，pytest parametrized | 17 条（`benchmarks/memory_cases.jsonl`） |

## 7. 已知限制

- 相关性打分是确定性 token 重叠（离线、可复现），不是向量相似度；接线时可在
  Read Gate 前加 embedding 召回，但过滤规则（隔离/过期/superseded）必须保留。
- 注入/敏感模式为正则清单，需随红队样本持续扩充；确认过的文本不再做注入拦截
  （用户显式确认优先）。
- 未接线：`radiant_llm.py` / `session_store.py` 仍走旧路径，由主控统一切换。
