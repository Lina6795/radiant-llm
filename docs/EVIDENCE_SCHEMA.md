# Evidence Schema（RADIANT-Control M1）

- 日期：2026-09-22
- 状态：已稳定（M1 验收门通过）
- 实现：`app/evidence/models.py`（Pydantic v2）、`app/evidence/adapter.py`、`app/evidence/store.py`（SQLite）

本文定义 Evidence Schema 的字段、版本与指纹规则、degraded 语义。适配器把上游
Visual-Parser 管线的三分离 JSONL（`01_chunks_kb.jsonl` / `02_visuals_kb.jsonl` /
`03_metadata_kb.jsonl`）转换为 Document + Evidence 记录并幂等落库。

## 1. 核心模型

### Document（文档版本级）

| 字段 | 类型 | 说明 |
|---|---|---|
| `document_id` | str | 上游约定：源文件名的 SHA-1 前 16 hex（与 `utils.vp_jsonl_writer.make_document_id` 逐字节一致） |
| `workspace_id` | str | 工作区隔离键，默认 `"default"` |
| `source` | str | 原始文件名（如 `paper.pdf`） |
| `content_hash` | str | 见 §2 |
| `document_version` | str | 见 §2，由 `content_hash` 派生 |
| `parser_fingerprint` | str | 见 §3 |
| `artifact_uri` | str | 原文件 URI（`file://...`）；不可解析时仍记录预期位置 |
| `artifact_status` | str | `"ok"` / `"missing"`：摄取时原文件是否可解析 |
| `page_count` / `pages` / `figures` / `metadata_fields` | — | 页面索引、图清单、成功元数据字段的文档级汇总 |
| `valid_from` / `valid_to` | datetime | 有效期；被新版本取代时旧版本 `valid_to` 被关闭，但记录保留可查 |

### Evidence（证据条目级，必需字段全集）

| 字段 | 类型 | 说明 |
|---|---|---|
| `evidence_id` | str | `ev-` + sha256(workspace_id \| document_version \| locator) 前 24 hex，确定性生成 |
| `workspace_id` / `document_id` / `document_version` / `content_hash` | str | 身份与归属（同 Document） |
| `page` | int \| null | 来源页码；无法定位页码时为空且 `degraded=True` |
| `section` | str \| null | 章节（上游 chunk 若有 `section` 字段则透传） |
| `figure_id` | str \| null | 形如 `docid:p3:f0` |
| `region_bbox` | RegionBBox \| null | `{x0,y0,x1,y1,unit}`；当前上游 02 文件不产生 bbox |
| `modality` | enum | `text` / `visual` / `metadata` |
| `source_span` | SourceSpan \| null | 定位信息：`chunk_id` 及前后各 1 个相邻 `chunk_id`（`prev_chunk_id` / `next_chunk_id`，邻居语义见下）、`figure_index`、字符偏移预留 |
| `artifact_uri` | str | 原文件 URI，继承自 Document |
| `parser_fingerprint` | str | 见 §3 |
| `authority_level` | str | `primary`（text）/ `supporting`（visual、metadata） |
| `valid_from` / `valid_to` | datetime | 随所属文档版本 |
| `degraded` | bool | 见 §4 |
| `degraded_reason` | str \| null | 机器可读原因，分号分隔多原因 |
| `content` / `metadata_fields` | — | 文本内容 / 图描述 / 元数据 JSON |

### Page / Figure

Document 内的轻量索引：`Page{page, chunk_ids[], figure_ids[]}`；
`Figure{figure_id, page, region_bbox, description, degraded, degraded_reason}`。

### 相邻 chunk 邻居语义（prev_chunk_id / next_chunk_id）

- 邻居按**文档级阅读序**计算：同一文档的全部 chunk 先按 `(page: int,
  chunk_index: int)` 数值排序，取排序后位置的前后各 1。
- **禁止**用 `chunk_index` 做全文档查找键或按 `chunk_id` 字符串字典序排序：
  上游 `chunk_index` 每页从 0 重计，`chunk_id` 中页码是字符串
  （`"p1" < "p10" < "p12" < "p2"`），两种做法都会在 ≥10 页文档中跨页错位。
- 跨页边界：页首 chunk 的 `prev_chunk_id` 链接**上一页最后一个** chunk（阅读序连续），
  页尾 chunk 的 `next_chunk_id` 链接下一页第一个 chunk；全文档第一个 chunk
  `prev_chunk_id=None`，最后一个 `next_chunk_id=None`。
- 页内 chunk_index 有缺口（如 c2 之后是 c5）时按位置相邻处理，即 c2 的 next 是 c5。
- 文档内只有一个 chunk 时 prev/next 均为 None。

## 2. 版本与 content_hash 规则

- `content_hash`：原文件可解析时为**源文件字节的 SHA-256**；原文件不可解析时退化为
  该文档全部解析记录 canonical JSON 的 SHA-256（仍保证内容变 → hash 变）。
- `document_version = "v-" + content_hash[:12] + "-" + sha256(parser_fingerprint)[:6]`。
  由 content_hash 派生；同名文件内容变化 → 新版本，**旧版本不覆盖、保留可查**
  （仅 `valid_to` 被关闭）。同字节但不同解析栈（见 §3）也落入不同版本，避免
  不同 parser 产物互相顶掉。
- 文档级幂等（修复 BC-parser-002 在 adapter 层的对策）：相同
  `content_hash + parser_fingerprint` 的文档重复摄取**短路，新增记录数为 0**。
  幂等键落库于 `ingest_state(workspace_id, document_id, content_hash, parser_fingerprint)`。

## 3. parser_fingerprint 规则

格式：`evidence-adapter/1;extractor=<nougat|lightweight|unknown>;vlm=<model|unknown>`

- `extractor` 取自 01 chunk 行的 `extractor` 字段，区分 nougat / lightweight 两条文本抽取路径；
- `vlm` 为产生 02（图表描述）/ 03（元数据）记录的 VLM 型号，由调用方显式传入
  （`ingest_directory(..., vision_model=...)`），未知时为 `unknown`；
- 前缀为 adapter 自身版本，schema/适配逻辑升级时可整体区分旧证据。

## 4. degraded 语义

原则：**不完整 = degraded，绝不静默变成权威证据。**

触发条件（`degraded=True` 并写明 `degraded_reason`）：

| 条件 | reason |
|---|---|
| visual 记录缺 `region_bbox` | `missing_region_bbox` |
| visual 记录缺 `figure_id` | `missing_figure_id` |
| 任何记录无法定位到 source+page | `unlocatable_source_page` |
| metadata 记录含 `_error` | `metadata_extraction_error: <原始错误>` |

查询纪律：`EvidenceStore.query_evidence` 与 `GET /evidence` **默认排除 degraded**
（即默认集合 = 权威回答候选）；仅当显式 `include_degraded=true` 或
`degraded=true` 过滤时才返回。

不视为单条 degraded、但显式标记的另两类情况：

- **artifact 不可解析**：原 PDF 被移动/删除时，Document `artifact_status="missing"`，
  且 `verify_provenance()` 逐版本返回 `{status: "missing", detail: "artifact_uri
  unresolvable: no file at ..."}`，错误路径明确、不静默；
- **无法归属任何文档的行**（既无 `document_id` 也无 `source`）：进入
  `AdapterResult.rejected_rows`，随摄取摘要显式返回，不丢弃不静默。

## 5. 摄取状态机与只读 API

- 每文档版本在 `ingest_state` 中跟踪 `pending/done/failed`：失败隔离（单文档失败不影响
  其他文档），重跑只续传 `pending/failed`，`done` 且指纹未变的短路。
- 只读 Evidence Inspector API（`app/api.py` 追加，依赖注入 store，与全局 `cb` 单例无关）：
  - `GET /evidence`：过滤 `document_id` / `modality` / `degraded` / `workspace_id`，
    `include_degraded`、`limit`(1–1000) / `offset` 分页；
  - `GET /evidence/{evidence_id}`：单条；
  - `GET /documents`：全部文档版本（含被取代的旧版本）。
- 存储路径：环境变量 `RADIANT_EVIDENCE_DB` 优先；否则
  `RADIANT_LLM_CONFIG_DIR/evidence.db`；再否则 `cwd/evidence.db`。无硬编码路径。
