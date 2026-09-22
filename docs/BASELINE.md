# RADIANT-LLM 原始系统基线分析书（修改前 as-is）

> 版本：v1.0 ｜ 日期：2026-09-22 ｜ 作者：Kimi Code（M0 审计）
> 用途：记录"魔改之前这个系统到底是什么"，与 `paper.md`（RADIANT-LLM 论文）逐节对应。
> 读者在阅读本文后再审阅任何改造 diff，就能分清：哪些是论文宣称、哪些是上游代码已有、哪些是 fork 既有增量、哪些是 RADIANT-Control 新增。
> 配套文件：`docs/UPSTREAM_AUDIT.md`（归属与许可）、`RADIANT-Control_执行跟踪与数据飞轮.md`（台账）。

---

## 1. 基线定义：这份"原始"具体指什么

| 项 | 值 |
|---|---|
| 上游来源 | Docker 镜像 `zev94/radiant-llm:latest`（论文配套实现），逐层拉取解包，非 git clone |
| 本仓库基线 commit | `6ef63f2` "baseline: 官方镜像原版（无 Docker 直跑）" |
| fork 既有增量（用户已完成，不属于本次 Control Plane 改造） | `4a7b176` 技能库合并进 `app/radiant_llm_skills`；`f09635d` MANIFEST 签名脚本；`051d0b8` gitignore |
| 分析时点 HEAD | `051d0b8` + 未提交的环境修复（`vp_nougat_engine.py` HF 变量名兼容、`.env` 镜像配置） |
| 运行环境 | 无特权云容器，Docker 不可用；`runtime/bin/python3.12`（镜像 `/usr/local` 解包）；6 核 CPU / 30G 内存 / 无 GPU；/mnt 余量 6.8G |
| 网络约束 | huggingface.co、arxiv.org、osti.gov 直连超时；`hf-mirror.com`、`export.arxiv.org` 可达 |
| 可用凭据 | `OPENAI_API_KEY` ✓、`HF_API_KEY` ✓、`VLLM_API_KEY`（无服务）；GEMINI/TAVILY/LANGCHAIN 未配 |

**重要边界**：论文 §4 的全部实验数字（CoP/CiP/CiH/HR/ViR、250 源扩展实验）是论文作者在 TAMU 环境、用 GPT-5.x 系列模型、专家人工评分得到的。本仓库**不含**这些实验的数据、评分 YAML、benchmark 问题集，也**不能复现**这些数字。它们只能作为"论文声明"引用，永远不能写进本项目的成果。

---

## 2. 论文宣称 vs 代码实现（逐节对照）

### 2.1 论文 §1.2 五项贡献 → 代码核对

| # | 论文宣称 | 代码实现情况 | 结论 |
|---|---|---|---|
| i | 本地优先、安全可部署框架，数据不出本机 | `app/api.py` FastAPI 单体 + 本地工作目录；但 LLM 走 OpenAI/Gemini API 时 prompt（含检索片段）会出本机；`LANGCHAIN_TRACING_V2=true` 硬编码（`radiant_llm.py:180`），不配 key 时行为未定义，且默认把 trace 发往 LangSmith 云端——与"local-first"宣称存在张力 | 部分实现，有反例 |
| ii | 多模态 Visual-RAG：文本+公式+表格+图表 | 三分离 JSONL 解析管线真实存在（`app/utils/vp_pipeline.py`），Nougat + VLM 图表描述 + PyMuPDF 降级 | 实现（质量待测） |
| iii | 动态 KB 扩展（种子文档→解析引用→自动下载） | `FileDownloaderTool` + Web 搜索工具存在；OSTI 批量下载是论文环境的专用脚本，**不在本仓库** | 部分实现 |
| iv | 引用强制（标题/作者/页码/图号）+ 3S 对齐 | 只有两层：(a) 系统提示词文字规则（`system_prompt_radiant_llm.yml:113` "Cite Sources"）；(b) RAG 工具内部 `RetrievalQAWithSourcesChain` 返回串里拼 sources（`pdf_helpers.py:1259,556`）。**没有结构化引用对象、没有逐句校验、没有拒答逻辑代码化** | 提示词级实现 |
| v | 聚焦 3S 主题管理 | 系统提示词 + fork 增量技能库（`app/radiant_llm_skills/`，6 个 Tier-1 领域包，prompt 注入式） | 提示词级实现 |

### 2.2 论文 §3.1 四阶段管线 → 代码核对

| 论文阶段 | 公式 | 代码位置 | 实况 |
|---|---|---|---|
| Stage 1 多模态解析 | (1) Nougat 文本/公式/表格；(2) VLM 图表描述；(3) VLM 元数据 | `vp_nougat_engine.py`（transformers 直调 `facebook/nougat-small`，非官方 nougat 包）；`vp_pipeline.py:189-249` Nougat 失败**静默降级** PyMuPDF；VLM 用 GPT-4o/gemini-2.5-flash（`tools/pdf_tools.py:72-91`） | 三份 JSONL：`01_chunks_kb.jsonl`（500 字块/100 重叠，`vp_config.py:75-78`）、`02_visuals_kb.jsonl`、`03_metadata_kb.jsonl` + `04_processed_pdfs` 注册表 + `05_pipeline.log` |
| Stage 2 语义分块+嵌入 | (4) 文本/视觉分别嵌入、统一索引 | ChromaDB 持久化（`{gpt,gemini,local}_vector_store/`），embedding 可切 OpenAI/Google/本地 bge-base-en-v1.5 | 固定 500/100 分块，**不是**论文说的"semantic chunking"，是定长分块 |
| Stage 3 相似度检索 | (5)(6) 余弦 top-k，文本∪视觉 | `similarity_search(k=20)`（`pdf_helpers.py:236-250`），纯 dense，**无 BM25/RRF/rerank/过滤** | 单路召回 |
| Stage 4 生成+agentic 控制 | (7) 生成；(8) 视觉证据缺失时拒答 | LangChain `create_tool_calling_agent` + `AgentExecutor`（`radiant_llm.py:1602-1612`）；拒答只有提示词文字，**式 (8) 的强制拒答在代码中不存在** | 无控制层 |

### 2.3 论文 §3.2 五项评测指标 → 代码核对

| 指标 | 论文定义 | 代码实现 |
|---|---|---|
| CoP 语义+数值正确性 | 式 (10)-(12) | **零实现** |
| CiP 引用精确率 | 式 (13) | **零实现** |
| CiH 锚点命中 | 式 (14) | **零实现** |
| HR 幻觉率 | 式 (15) | **零实现** |
| ViR 视觉召回 | 式 (16) | **零实现** |

论文自己说明这些指标依赖专家人工判断（§3.2 "expert verified"），评分手工 YAML 未随代码发布。本仓库连自动评测骨架都没有：无 `tests/`、无 `benchmarks/`、无 eval 脚本。`app/basic_queries_radiant_llm.yml` 只是前端示例问题。

### 2.4 论文 §3.3 工具表（Table 1）→ 代码核对

论文 8 个工具 vs 代码 13 个 `@tool`（`radiant_llm.py:457-469`）：

| 论文工具 | 代码对应 | 差异 |
|---|---|---|
| PDFReaderTool | ✓ 同名 | 一致 |
| ImageAnalysisTool | ✓ 同名 | 一致 |
| CSVDataQueryTool / LightWeightCSVDataTool | `CSVDataFinderTool`（pandas agent）+ `CSVandExcelFileParserTool` | 命名重构 |
| Web Search Tools | `WebSearchTool`（Tavily→Google CSE 回退）+ `WebScraperTool` + `WikipediaSearchTool` | 拆分更细 |
| FileDownloaderTool | ✓ 同名 | 一致 |
| PythonREPLTool | ✓ langchain_experimental PythonREPL | **无沙箱**，直接执行任意代码 |
| LightWeightPDFReaderTool | 合并进 `PDFReaderTool` 的降级路径 | 合并 |
| —（论文没有） | `URLValidationTool`、`TextFileReaderTool`、`SkillLookupTool`、`PDFKnowledgeBaseSanitizerTool`、会话搜索工具 | 上游/fork 新增 |

### 2.5 论文 §3.4 数据源 → 代码核对

论文用 OSTI.gov 1000 篇元数据 CSV 精选 250 篇建 3S KB。本仓库**不含**该 CSV、不含任何预建 KB、`Docker_Executable/data/` 为空。OSTI 批量下载脚本不在仓库。任何"3S 知识库"都需要自己重新构建——且本环境 osti.gov 不可达。

### 2.6 论文 §3.5 安全与隐私 → 代码核对

| 论文宣称 | 代码实况 |
|---|---|
| 原始数据不出本机 | 本地存储属实；但 LLM API 调用会带出 prompt，LangSmith trace 默认强制开启 |
| 用户完全控制 KB | ✓ 工作目录由用户选 |
| 不泄露机密信息、不提供核扩散指导 | 仅系统提示词文字 + fork 技能库的两级注入扫描（`skill_security.py`，Tier-A 正则 + Tier-B LLM judge） |
| Web 内容默认不入库 | ✓ 与论文一致 |

### 2.7 论文 §4 实验结果 → 与本仓库的关系

- §4.2 页级 benchmark（3 页 15 问）：页面、问题、评分 YAML **均未发布**，无法复现。
- §4.3 与 ChatGPT/TAMU Chat/冻结模型对比：依赖特定平台账号与时间点，不可复现。
- §4.4 上下文扩展（1→250 源）：250 篇 3S 语料不在仓库，UNFSF 报告（LA-UR-14-27045）需自行从 OSTI 获取（本环境 OSTI 不可达，需找替代渠道）。
- 100 源 R1 退化 / R2 恢复（Table 6）：只能表述为"论文报告了一次随机性失败窗口"，不得引用于论证"必然崩溃"。

**结论：论文数字全部是"论文声明"，本项目的一切指标必须自己测。**

---

## 3. 运行时架构实况（论文没写的部分）

```text
用户 → FastAPI (api.py)
        ├─ POST /query ──────────────┐
        ├─ GET /stream-query (SSE) ──┤  全局单例 cb = Chatbot()（radiant_llm.py:2220）
        │                            ↓
        │              convchain_api（radiant_llm.py:2027）
        │   工作目录提示 → 会话历史检索注入(TF-IDF/fuzzy) → 压缩记忆注入
        │   → 技能路由注入(hybrid 打分) → AgentExecutor.invoke
        │   → LLMMarkdownParser 二次润色 → 返回
        │                            ↓
        │              13 个工具 → Chroma / Web / REPL / VLM
        ├─ /sessions/*  会话 JSONL 持久化（session_store.py）
        ├─ /skill-*     技能库管理（MANIFEST.json SHA-256 完整性校验）
        └─ /*           React dist 托管（前端源码不在仓库）
```

**fork 既有增量（用户已完成的工作，改造前的"已有基础"）**：

1. **领域技能库**：Tier-0 全局策略 + 6 个 Tier-1 包（safety-pra / security-cyber / safeguards-mca / geniv-reactors / digital-twin / regulatory）；hybrid 打分路由注入；`SkillLookupTool` 按需拉全文；两级注入攻击扫描；MANIFEST.json 签名防篡改（`maintenance_scripts/generate_skill_manifest.py`）。
2. **无 Docker 原生运行方案**：镜像层逐层解包 + SHA256 校验 + `start_radiant.sh`（`部署记录-无Docker运行.md`）。
3. Grace vLLM 接入代码（上游移植，本环境不可用）。

**上游自带的工程兜底**（非论文内容）：迭代上限 40 / 600s（`radiant_llm.py:590-592`）、迭代将尽收尾 nudge、cap-out 合成总结、context overflow 压缩重试（keep_last 2→1）、70%/85% 上下文阈值（`chars//4` 粗算）。

---

## 4. 已确认缺陷清单（改造的" before "）

代码审计证实（详细证据见台账第 13.2 节 Bad-case Registry）：

| 缺陷 | 证据 | 论文是否承认 |
|---|---|---|
| 无意图路由/计划/授权层，所有 query 直进 AgentExecutor | `radiant_llm.py:2027` | 论文 §3 宣称有 supervisor 控制，代码无对应实现 |
| 全局单例，并发请求共享状态，SSE 轮询共享列表 | `radiant_llm.py:2220`、`api.py:426-488` | 未涉及 |
| 纯 dense top-20，无 BM25/RRF/rerank | `pdf_helpers.py:236-250` | 论文未实现 hybrid |
| 无 checkpoint/状态机/幂等，重试仅覆盖 context overflow | `radiant_llm.py:2092-2155` | 未涉及（论文无 Durable Runtime） |
| token 估算 chars//4 | `radiant_llm.py:669-682`（注释自承"rough"） | 未涉及 |
| citation 靠提示词自觉，无 claim-evidence 校验；二次润色可能引入新 claim | `system_prompt_radiant_llm.yml:113`、`radiant_llm.py:2198` | 论文宣称引用强制，代码无强制 |
| PythonREPLTool 无沙箱 | langchain_experimental 默认 | 未涉及 |
| 会话历史无写入门控（TF-IDF/fuzzy 直接注入） | `session_store.py:246-327` | 未涉及 |
| 零测试、零评测、论文五指标零实现 | 全仓库 glob | 论文有指标定义无代码 |
| Nougat 静默降级 PyMuPDF 不可见 | `vp_pipeline.py:189-249` | 未涉及 |
| docker-compose 写死 `/mnt/lina` 路径 | `docker-compose.yml:44,47` | — |

---

## 5. 改造起点声明

**"原始基础"= 上游镜像代码（§2 全部）+ fork 既有增量（§3 技能库/原生运行）。**

RADIANT-Control 改造（M1–M10）将从以下零点起步：

- 控制层：无 → 新建 `app/control/`
- Durable Runtime：无 → 新建 `app/durable/`（命名避开根目录 `runtime/` 解释器）
- Hybrid 检索：无 → 新建 `app/retrieval/`（A0 基线 = 现有 dense top-20）
- 精确预算：chars//4 → tiktoken + 分项预算
- Memory 治理：会话 JSONL → Write/Read Gate + 分类
- Claim 校验：无 → 新建 `app/evidence/` + Verifier
- 评测：零 → 新建 `app/eval/` + `benchmarks/` + `tests/`

本文之后的一切改动，diff 均可对照本分析书定位"改了哪一层"。

---

## 6. M0 实测回填（2026-09-22，产物 `artifacts/baseline/m0-20260922/`）

- [x] **Nougat 真实可用**：`facebook/nougat-small` 经 hf-mirror 下载，CPU 解析 15 页用时 615.4s（约 41s/页），119 个 chunk 全部 `extractor="nougat"`，**未走 PyMuPDF 降级**。16 条 ERROR/WARNING 全部属于视觉调用失败，与 Nougat 无关。
- [x] **Schema profiling**（`schema_profile.json`）：`01_chunks` 7 字段填充率 100%、15 页全覆盖、chunk 长度 13–499（均值 344）；`02_visuals` 未生成（视觉 API 不可用）；`03_metadata` 写入的是 `_error` 记录（失败隔离有效）。
- [x] **端到端实测**：`POST /initialize`(deepseek-v4-pro) → `/directory` → `/query`，链路 chat → PDFReaderTool → Chroma(本地 bge) → 页级 sources → 带引用回答，全程真实跑通（`e2e_BL-T01.json`）。
- [x] **摄取幂等性实测**：重复摄取不短路（重算 119 chunk）但按 chunk_id 覆盖写、无重复记录（详见台账 BC-parser-002）。
- [ ] 视觉链路（02_visuals/03_metadata 正常产物）：等 `GEMINI_API_KEY` 到位后补测。
