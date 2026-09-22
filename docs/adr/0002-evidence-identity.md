# ADR 0002：以 content_hash + parser_fingerprint 作为证据身份

- 日期：2026-09-22
- 状态：已接受
- 关联：docs/EVIDENCE_SCHEMA.md；修复对策 BC-parser-002（上游 run_pipeline 重复摄取不短路）

## 背景

上游 RADIANT-LLM 解析管线（Nougat/lightweight 文本 + VLM 图表与元数据）产出三分离
JSONL。M0 实测确认两个事实：

1. `document_id` 只是**文件名的 SHA-1 前 16 位**——同名不同内容的文件会得到同一个
   `document_id`，无法区分版本；
2. 上游重复摄取不短路（重算但覆盖写），下游若直接以 `document_id` 或行内容做幂等键，
   要么被同名不同内容顶包，要么无法识别"同内容重跑"。

同时，同一份字节用不同解析栈（nougat vs lightweight，或不同 VLM 型号）产出的证据
质量与语义不同，不能混为一谈，也不应互相覆盖。

## 决定

证据身份 = **content_hash + parser_fingerprint**：

- `content_hash`：源文件字节 SHA-256（文件不可解析时退化为该文档解析记录 canonical
  JSON 的 SHA-256）。内容变 → hash 变。
- `parser_fingerprint`：`adapter版本;extractor=...;vlm=...`，区分文本抽取器与 VLM 型号。
- `document_version` 由 content_hash 派生（拼入指纹短哈希后缀），同名不同内容 →
  新版本，旧版本保留可查；同字节不同解析栈 → 不同版本，互不覆盖。
- 幂等键 `(workspace_id, document_id, content_hash, parser_fingerprint)` 落库于
  `ingest_state`：重复摄取短路（新增 0），失败仅续传。

## 备选方案与取舍

- **仅用 document_id（文件名哈希）**：无法区分同名不同内容，否决。
- **仅用内容哈希**：无法区分同字节不同解析栈的产物（nougat 与 lightweight 结果会被
  判为同一批证据），否决。
- **UUID 随机版本号**：破坏确定性，同一批产物重跑会生成新身份，幂等无法实现，否决。
- **以上游行内容（如 chunk_id 集合）做哈希**：不要求原文件在场、可离线工作，但作为
  content_hash 的**退化路径**保留；主路径仍用文件字节，因为字节哈希与解析产物无关，
  能在"解析参数变了但源没变"与"源变了"之间做出更干净的区分。

## 后果

- adapter 层自行实现真正的文档级幂等，不依赖上游修复 BC-parser-002；
- 旧版本证据永久可查（`valid_to` 仅标记有效性窗口），支撑后续 claim-evidence 校验
  与回放审计；
- 解析栈升级（换 extractor、换 VLM、adapter 版本号递增）自动形成新版本线，不会
  污染既有证据；
- 代价：同字节多解析栈并存会多占存储，可在后续 milestone 增加版本保留策略。
