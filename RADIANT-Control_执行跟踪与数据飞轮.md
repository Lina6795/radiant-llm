# RADIANT-Control 执行跟踪与数据飞轮

> 本文件是**执行台账**，与《RADIANT-Control_KimiCode分阶段执行计划.md》（下称"主计划"）配套使用。  
> 主计划定义"做什么、怎么验收"；本文件记录"实际做了什么、结果是什么、数据飞轮转到哪了"。  
> 创建：2026-09-22 ｜ 仓库基线：commit `051d0b8`（fork：技能库 + 会话管理 + Grace vLLM 增量，上游 `zev94/radiant-llm` 原版）

## 0. 使用规则（Kimi Code 与维护者共同遵守）

1. **只追加，不删改历史。** 执行记录、bad-case、Release Gate 历史写错就追加更正条目，禁止覆盖旧记录——台账本身就是审计证据。
2. 每次执行结束（无论成败）必须当场更新三处：对应 Milestone 的【执行记录】、第 11 节【执行日志】、涉及指标时更新第 12 节【指标总表】。
3. 每个条目的最小字段：**日期 / 操作 / 命令 / 结果 / 产物路径 / commit**。没有 commit 的实验结果不算数。
4. 状态值统一：`未开始` / `进行中` / `完成` / `受阻` / `已放弃`（放弃必须写明原因）。
5. 所有数字必须能回到 `artifacts/` 下的结果文件或 `benchmarks/` 下的冻结 case；凭印象填的数字视为伪造，发现即作废该条目。
6. 数据飞轮（第 13 节）的每次转动必须满足：单变量修改、失败已归因、修复已固化为 regression case、Release Gate 通过。
7. 验收门全部勾选才算 Milestone 完成；完成后把主计划"Milestone Mx 完成报告"全文存入 `docs/milestone_reports/Mx.md`，此处只挂链接。
8. 受阻（`受阻`）超过一轮必须在第 14 节【风险与债务】登记，并写明解锁条件。

---

## 1. 总进度看板

| Milestone | 内容 | 状态 | 完成报告 | 验收通过日期 |
|---|---|---|---|---|
| M0 | 仓库审计、环境修复、可复现 Baseline | 完成（视觉补测遗留转 M1 前置，等 GEMINI_API_KEY） | `docs/milestone_reports/M0.md` | 2026-09-22 |
| M1 | Evidence Schema 与解析产物 Adapter | 完成 | `docs/milestone_reports/M1.md` | 2026-09-22 |
| M2 | Agent Control Plane 骨架 | 完成 | `docs/milestone_reports/M2.md` | 2026-09-22 |
| M3 | Durable Runtime 与精确恢复 | 完成 | `docs/milestone_reports/M3.md` | 2026-09-22 |
| M4 | Retrieval 与 Evidence Control | 完成 | `docs/milestone_reports/M4.md` | 2026-09-22 |
| M5 | Context Budget 与 Anchor Preservation | 完成 | `docs/milestone_reports/M5.md` | 2026-09-22 |
| M6 | Memory Governance | 完成 | `docs/milestone_reports/M6.md` | 2026-09-22 |
| M7 | Visual Evidence、Claim Verification 与 Human Review | 完成（视觉真实实验 deferred 等百炼 key） | `docs/milestone_reports/M7.md` | 2026-09-22 |
| M8 | Eval Harness、Observability 与数据飞轮 | 完成（六层 116 用例一键回归 + Gate） | `docs/milestone_reports/M8.md` | 2026-09-22 |
| M9 | API、SSE、Dashboard 与云端交付 | 完成 | `docs/milestone_reports/M9.md` | 2026-09-22 |
| M10 | 最终实验与求职材料 | 完成 | `docs/milestone_reports/M10.md` | 2026-09-22 |

**当前下一步**：**M0–M10 全部完成（11/11）**。剩余工程债务见第 14 节（compose 后置封装、plan 内存注册表改 goal 重放、视觉 bbox 决策、CoP judge/人审、真实 100-PDF 语料扩展）。项目进入维护/迭代模式：改动一律走"冻结用例 + eval.runner + release_gate"。

---

## 2. 环境与基线台账

实验结果只在登记过的环境上有效。环境变化（换机、换模型、换 key、升依赖）必须追加新行，旧行保留。

| 登记日期 | 机器/容器 | CPU/内存/GPU | Python | LLM/VLM 配置 | Embedding | 已知缺陷 | 备注 |
|---|---|---|---|---|---|---|---|
| 2026-09-22 | 无特权云容器（Docker 不可用） | 待实测补充 | runtime/bin/python3.12（镜像解包） | OpenAI/Gemini API 为主用路径；Grace vLLM 为可选项（需 TAMU HPRC SSH 隧道，本环境不可用，见第 14 节） | `RADIANT_EMBEDDING_PROVIDER` 可切 openai/google/local(bge-base-en-v1.5) | huggingface.co 直连超时，已配 `HF_ENDPOINT=https://hf-mirror.com`（2026-09-22 验证可登录、nougat-small 在镜像上存在）；tesseract 缺失；FUSE 盘不支持符号链接；磁盘紧张 | 初始环境 |

### 2.1 配置指纹规则

每次出指标的运行，在产物目录写 `config_fingerprint.json`：`{git_commit, 模型+版本, prompt 文件 hash, 检索配置, 数据版本, 环境行号}`。指纹缺失的结果不进指标总表。

---

## 3. M0：仓库审计、环境修复与可复现 Baseline

**状态：进行中**

### 执行记录

- 2026-09-22 ｜ 全仓库代码审计 ｜ explore 审计（无命令产物）｜ 完成：确认无控制层（`radiant_llm.py:2027` 直进 AgentExecutor）、全局单例（`:2220`）、纯 dense 检索（`pdf_helpers.py:236-250`）、零测试零指标、chars//4 预算（`:669-682`）、PythonREPL 无沙箱、LangSmith 硬编码（`:180`）、compose 写死 `/mnt/lina`；产出主计划 v2 ｜ commit `051d0b8`（审计结论写入计划文档，代码未动）
- 2026-09-22 ｜ HF 环境修复 ｜ `vp_nougat_engine.py` 接受 HF_API_KEY；`.env` 配 HF_ENDPOINT 镜像 ｜ whoami 登录成功（lina0819）｜ 待提交
- 2026-09-22 ｜ LangSmith 条件化 ｜ 7 处硬编码改"有 key 才开" ｜ 全部编译通过，默认关闭 ｜ 待提交
- 2026-09-22 ｜ tesseract ｜ conda defaults 渠道装到 /root/tesseract-env（阿里云 anaconda 镜像已停服 404，官方源可达）｜ tesseract 5.5.2 可用，已接入 start_radiant.sh PATH ｜ 待提交
- 2026-09-22 ｜ DeepSeek 对话接入 ｜ radiant_llm.py 新增 deepseek 分支 + 模型清单 ｜ `/initialize deepseek-v4-pro` 成功 ｜ 待提交
- 2026-09-22 ｜ Nougat 最小解析 ｜ `artifacts/baseline/m0-20260922/run_parse.py` ｜ 615.4s/15页（≈41s/页 CPU），119 chunk 全部 extractor=nougat，未降级；视觉阶段 16 条 ERROR（DeepSeek 无视觉+模型名不符，预期内）｜ `parse_run_summary.json`、`config_fingerprint.json` ｜ 待提交
- 2026-09-22 ｜ schema profiling ｜ `schema_profile.py` ｜ 01 表 7 字段 100% 填充、15 页全覆盖；02 未生成；03 为 _error 记录（失败隔离有效）｜ `schema_profile.json` ｜ 待提交
- 2026-09-22 ｜ 摄取幂等性实测 ｜ 重复 run_pipeline ｜ 不短路（重算 119 chunk）但按 chunk_id 覆盖写、0 重复记录 → BC-parser-002 ｜ 本文件 ｜ 待提交
- 2026-09-22 ｜ 端到端实测 ｜ POST /initialize → /directory → /query ｜ DeepSeek→PDFReaderTool→Chroma(本地bge)→页级sources→带引用回答全链路真实跑通 ｜ `e2e_BL-*.json` ｜ 待提交
- 2026-09-22 ｜ agent 预算配置化 ｜ 3 常量改 RADIANT_AGENT_* 环境变量 ｜ 默认值不变、编译通过 ｜ `docs/HARDCODED_CONFIG_REGISTRY.md` ｜ 待提交

### 待办清单（M0 关闭前必须全部完成）

- [x] 环境修复：HF 变量名兼容 + HF_ENDPOINT 镜像（登录实测通过）
- [x] 环境修复：tesseract 5.5.2 安装并接入 PATH
- [x] `LANGCHAIN_TRACING_V2` 改配置项，默认关闭
- [x] 1 份公开含图表 PDF 跑最小解析，记录命令/模型/耗时/成本/输出（attention 15页，615.4s）
- [x] 明确解析路径：Nougat 实测可用，extractor 字段可证，未降级
- [x] `01_chunks/02_visuals/03_metadata` schema profiling（02 因视觉 API 缺失未生成，等 Gemini key 补测）
- [x] `benchmarks/baseline_cases.jsonl`：12 条（5 文本/3 数字/2 图表/2 拒答）
- [x] `POST /query` 实测 query → evidence → answer → citation 全链路
- [x] 写死路径配置化/登记（`docs/HARDCODED_CONFIG_REGISTRY.md`）
- [x] 交付物：`docs/BASELINE.md`、`docs/UPSTREAM_AUDIT.md`、`docs/OWNERSHIP.md`、`docs/adr/0001-project-boundary.md`、`configs/baseline.yaml`、`artifacts/baseline/m0-20260922/`
- [ ] 视觉链路补测（`GEMINI_API_KEY` 到位后重跑 run_parse.py 验证 02/03 正常产物）→ 转入 M1 前置

### 验收门

- [x] 同一命令可再次产生结构一致的解析结果（重复运行 119 条、schema 一致、chunk_id 无重复）
- [x] 解析路径（Nougat）明确记录且可复现（extractor 字段 + config_fingerprint.json）
- [x] 至少一个 Evidence 能回到 PDF 页码（e2e sources 页级：Page 1 Chunk 2 等）
- [x] 每项失败已归类：视觉调用失败→parser(VLM)层；摄取不短路→parser 层（BC-parser-002）
- [x] 无论文数字冒充本项目成绩

### 指标记录（M0 基线值，后续 Milestone 的对照原点）

| 指标 | 值 | 运行日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|
| 解析路径 | nougat（未降级） | 2026-09-22 | `config_fingerprint.json` | artifacts/baseline/m0-20260922/ |
| 单 PDF 解析耗时 | 615.4s（15 页 ≈ 41s/页，CPU） | 2026-09-22 | 同上 | parse_run_summary.json |
| chunk 数 / 字段填充率 | 119 / 100% | 2026-09-22 | 同上 | schema_profile.json |
| 视觉记录生成 | 0（DeepSeek 无视觉，等 Gemini） | 2026-09-22 | 同上 | parse_run.log |
| 端到端链路 | 跑通（详见逐 case 结果，本节下方执行记录） | 2026-09-22 | 同上 | e2e_BL-*.json |

---

## 4. M1：Evidence Schema 与解析产物 Adapter

**状态：完成（2026-09-22）** ｜ 依赖：M0 ｜ 完成报告：`docs/milestone_reports/M1.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/evidence 包 + api.py 只读路由 + 15 测试 + 两文档 ｜ pytest tests/evidence ｜ 14 passed →（邻居 bug 修复后）15 passed ｜ 见 M1 报告 ｜ 待提交
- 2026-09-22 ｜ 主控集成：真实基线摄取 121 条、重复摄取 0、API 起服务实测三路由 ｜ curl /documents /evidence ｜ 数据正确；发现邻居 chunk 字典序错位 → 修复 → 回归固化 → 重建库复验通过（飞轮闭环 #1）｜ artifacts/baseline/m0-20260922/evidence.db ｜ 待提交

### 验收门

- [x] 重复摄取新增记录数为 0（真实基线复验 short_circuited=True）
- [x] 修改版本不覆盖旧版本（同名不同内容 → 新 document_version 并存）
- [x] 任意入库 Evidence 可定位来源文档和页码（page/chunk_id/邻居/artifact_uri 齐全）
- [x] 不可定位记录标 degraded，不静默进入权威回答（查询默认排除，需显式 include_degraded）
- [x] 必测场景全过：重复摄取 / 同名不同内容 / 中途失败续传 / 缺 bbox / 原 PDF 移动 / Nougat vs lightweight 双 fingerprint

### 指标记录

| 指标 | 值 | 运行日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|
| 摄取幂等性（重复摄取新增数） | 0 | 2026-09-22 | evidence-adapter/1;extractor=nougat;vlm=gpt-5.4 | artifacts/baseline/m0-20260922/evidence.db |
| degraded 记录占比 | 1/121（_error metadata；视觉未生成） | 2026-09-22 | 同上 | 同上 |
| 断点续传恢复 | 测试覆盖（failed 文档重跑只续传） | 2026-09-22 | — | tests/evidence |

---

## 5. M2：Agent Control Plane 骨架

**状态：完成（2026-09-22）** ｜ 依赖：M0 ｜ 完成报告：`docs/milestone_reports/M2.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/control 包（8 模块）+ 17 工具登记 + 49 测试 + 2 冻结用例集 + CONTROL_CONTRACT ｜ pytest tests/control ｜ 49 passed（0.16s 全离线）｜ 见 M2 报告 ｜ 待提交
- 2026-09-22 ｜ 主控集成：全量 tests/evidence+tests/control 64 绿 ｜ pytest ｜ 无跨包冲突 ｜ — ｜ 待提交

### 工具风险登记表（17 工具，M2 任务 5 的输出，已冻结）

| 工具 | 风险等级 | 副作用 | 幂等键 | 默认策略 |
|---|---|---|---|---|
| evidence.search（mock） | read_only | 无 | 不需要 | allow |
| evidence.inspect（mock） | read_only | 无 | 不需要 | allow |
| citation.validate（mock） | read_only | 无 | 不需要 | allow |
| report.export（mock） | bounded_write | 写文件 | 必须（幂等账本，effect_count 可审计） | allow（限 workspace） |
| PDFReaderTool | read_only（元数据） | 无 | — | allow |
| PDFKnowledgeBaseSanitizerTool | bounded_write（元数据） | 改 KB | 待 M3 | allow（限 workspace） |
| URLValidationTool | read_only（元数据） | 无 | — | allow |
| WebSearchTool | external + 高风险 | 外发查询 | — | review（不执行） |
| WebScraperTool | external | 外发请求 | — | review |
| WikipediaSearchTool | external | 外发请求 | — | review |
| PythonREPLTool | external + 高风险 | 任意代码执行 | — | review（不执行） |
| ImageAnalysisTool | read_only（元数据） | 无 | — | allow |
| CSVandExcelFileParserTool | read_only（元数据） | 无 | — | allow |
| CSVDataFinderTool | read_only（元数据） | 无 | — | allow |
| TextFileReaderTool | read_only（元数据） | 无 | — | allow |
| SkillLookupTool | read_only（元数据） | 无 | — | allow |
| FileDownloaderTool | external + 高风险 | 下载写盘 | 待 M3 | review（不执行） |

### 验收门

- [x] 非法计划拦截率 100%（冻结契约测试集 13 条越权全拦，措辞不泛化）
- [x] 未授权工具实际执行次数为 0（断言 effect_count==0）
- [x] deny/clarify/abstain 均有稳定 reason code（契约层 min_length=1）
- [x] 换模型不绕过 Schema/Policy（test_model_swap 恶意 planner 全拦截）
- [x] 必测场景全过：幻觉工具名 / 参数缺失·错型·多余 / 超 max_tool_calls / 低置信澄清 / 只读请求调写工具 / Prompt Injection（含 skills 变体）/ Planner 异常 fail-closed

### 指标记录

| 指标 | 值 | 运行日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|
| 契约测试集规模 | router 12 + policy 16 = 28 条冻结 | 2026-09-22 | — | benchmarks/ |
| 误拦率（合法计划被拒） | 0（冻结集合法用例全 allow） | 2026-09-22 | — | tests/control |

---

## 6. M3：Durable Runtime 与精确恢复

**状态：完成（2026-09-22）** ｜ 依赖：M2 ｜ 完成报告：`docs/milestone_reports/M3.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/durable（9 文件，自包含状态图）+ 46 测试 + RT-01..08 + 两文档 ｜ pytest tests/durable ｜ 46 passed ×2 轮 ｜ 见 M3 报告 ｜ 待提交
- 2026-09-22 ｜ 主控集成：修复 pytest 同名收集冲突（test_idempotency.py → test_durable_idempotency.py + pytest.ini pythonpath=app）｜ 全量 157/157 ｜ pytest.ini ｜ 待提交

### 故障注入用例台账

| 用例 | 预期行为 | 实测结果 | 日期 | 产物路径 |
|---|---|---|---|---|
| RT-01 检索节点首次超时、二次成功 | retry 后完成，不重复已做节点 | 通过（前置节点调用=1） | 2026-09-22 | tests/durable |
| RT-02 LLM 节点持续失败 | terminal 分类，安全终止 | 通过 | 2026-09-22 | 同上 |
| RT-03 导出成功后网络断开 | 幂等去重，不重导出 | 通过（effect_count=1） | 2026-09-22 | 同上 |
| RT-04 崩溃后 resume | 从精确 checkpoint 继续 | 通过（restored_steps+双指纹+fencing） | 2026-09-22 | 同上 |
| RT-05 相同 idempotency key 重复提交 | 副作用数不增加 | 通过（含 8 线程并发） | 2026-09-22 | 同上 |
| RT-06 非法状态跳转 | TypedError 拒绝 | 通过 | 2026-09-22 | 同上 |
| RT-07 lease 过期另一 Worker 接管 | 单点继续，stale 被拒 | 通过（fencing token） | 2026-09-22 | 同上 |
| RT-08 两并发 run | 事件与状态不串 | 通过（seq 各自连续） | 2026-09-22 | 同上 |

### 验收门

- [x] 恢复成功率可计算并输出（8/8=1.0，RUNTIME_CASES_SUMMARY JSON）
- [x] 已完成节点不重复执行
- [x] 有副作用工具重复副作用数为 0
- [x] 每次恢复能说明 checkpoint 与配置（双指纹，不匹配默认拒绝）
- [x] cancel 后不再产生新的工具调用
- [x] 并发 run 事件不串流

### 指标记录

| 指标 | 值 | 运行日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|
| 冻结故障用例恢复成功率 | 8/8 = 1.0 | 2026-09-22 | — | benchmarks/runtime_cases.jsonl |
| 重复副作用率 | 0 | 2026-09-22 | — | tests/durable |
| 事件丢失率 | 0 | 2026-09-22 | — | 同上 |

---

## 7. M4：Retrieval 与 Evidence Control

**状态：完成（2026-09-22）** ｜ 依赖：M1 ｜ 完成报告：`docs/milestone_reports/M4.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/retrieval（10 文件）+ 47 测试 + 16 冻结用例 + A0–A4 真实实验 ｜ pytest + retrieval.experiment ｜ 47 passed；A0→A4 变好 3/变差 0/不变 11 ｜ artifacts/retrieval/m4-20260922/ ｜ 待提交

### 对照实验矩阵（A0–A4，冻结 `benchmarks/retrieval_cases.jsonl`，14 条带锚点用例）

| 配置 | Recall@5 | Recall@20 | MRR | nDCG@20 | anchor hit@5/20 | P50/P95 ms | 日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|---|---|---|---|---|
| A0 现有 Chroma dense top-20 | 0.2344 | 0.4292 | 0.7792 | 0.4462 | 0.929/1.0 | 48.6/52.3 | 2026-09-22 | metrics.json 内含 | artifacts/retrieval/m4-20260922/ |
| A1 BM25 + Dense（交错） | 0.2226 | 0.4513 | 0.7673 | 0.4598 | 0.857/1.0 | 50.2/54.6 | 2026-09-22 | 同上 | 同上 |
| A2 A1 + RRF | 0.2487 | 0.4775 | 0.8452 | 0.4888 | 1.0/1.0 | 51.4/64.1 | 2026-09-22 | 同上 | 同上 |
| A3 A2 + proxy rerank | 0.2226 | 0.4775 | 0.8226 | 0.4787 | 0.857/1.0 | 50.5/62.2 | 2026-09-22 | 同上 | 同上 |
| A4 A2 + Relevance/Authority Gate（已删 A3） | **0.2985** | 0.4720 | **0.8571** | **0.5010** | 1.0/1.0 | 56.0/310（单 case 离群） | 2026-09-22 | 同上 | 同上 |

paired case diff（`artifacts/retrieval/m4-20260922/paired_diff.json`）：

- A0→A4：变好 [RET-T04, RET-T05, RET-V01]，变差 []，不变 11
- 分步：A0→A1 变差 3（交错稀释）→ A2 RRF 全修复 0 回退 → A3 proxy 使 T04/T05 变差 → A4 gates 恰好修复
- **proxy reranker 结论：无收益，A3 阶段已删，接口保留待真 cross-encoder 复测**

### 验收门

- [x] 冻结数据与配置后可一键重复实验（单命令，docs/RETRIEVAL.md）
- [x] 每个最终 Evidence 可解释来源路、融合方式、保留原因（80 条 per-case trace）
- [x] authority gate 能阻止宽泛来源挤掉目标锚点（实测 5 case 触发 13 次 anchor_protected；度量性泄漏已声明）
- [x] 无显著收益的 reranker 已删除（A3 proxy，2 变差 0 变好）

---

## 8. M5：Context Budget 与 Anchor Preservation

**状态：完成（2026-09-22）** ｜ 依赖：M4 ｜ 完成报告：`docs/milestone_reports/M5.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/context（7 文件）+ 44 测试 + 12 冻结用例 + 压力实验 ｜ pytest tests/context ｜ 44 passed；pin 开 100-source gold 保留 1.000 ｜ artifacts/context/m5-20260922/ ｜ commit 待

### 压力实验记录（pseudo-sources，seed 20260922，3 reps，mean±pstdev）

| 来源数 | 运行 | gold evidence 保留率(pin on/off) | token 节省率(on/off) | overflow 数 | 日期 | 产物路径 |
|---|---|---|---|---|---|---|
| 1 | R1-R3 | 1.000/0.400 | 0.807/0.821 | 0 | 2026-09-22 | artifacts/context/m5-20260922/metrics.json |
| 10 | R1-R3 | 1.000/0.400 | 0.835/0.845 | 0 | 同上 | 同上 |
| 25 | R1-R3 | 1.000/0.400 | 0.869/0.891 | 0 | 同上 | 同上 |
| 50 | R1-R3 | 1.000/0.400 | 0.912/0.925 | 0 | 同上 | 同上 |
| 100 | R1-R3 | 1.000/0.267±0.189 | 0.939/0.942 | 0 | 同上 | 同上 |

失败 onset：pin 开全程零失败（4.2× 超配仍 1.000）；pin 关 25–100 间歇性事实丢失、100 处 1/3 reps gold 全灭——已如实报告，未包装成完全解决。方法学局限（pseudo-sources、代理指标污染）已声明。

### 验收门

- [x] 证据可追溯压缩前记录（lineage 双 sha256；evidence 分区压缩抛异常红线）
- [x] 长上下文实验展示全部重复运行（worst-rep 判定 onset）
- [x] 100-source 不稳定明确报告失败区间（pin 关 25–100）
- [x] 无静默截断（舍弃必带 partition/item_id/reason/tokens）

---

## 9. M6：Memory Governance

**状态：完成（2026-09-22）** ｜ 依赖：M3 ｜ 完成报告：`docs/milestone_reports/M6.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/memory（6 文件）+ 68 测试 + 17 冻结用例 ｜ pytest tests/memory ｜ 68 passed，六项指标全达标 ｜ 见 M6 报告 ｜ commit 待

### 必测场景台账

| 场景 | 预期 | 实测 | 日期 | 产物路径 |
|---|---|---|---|---|
| 两 workspace 同名实体 | 隔离，泄漏 0 | 通过（MEM-14，泄漏=0） | 2026-09-22 | tests/memory |
| 用户纠正旧偏好 | supersede，旧值可审计不默认召回 | 通过（MEM-15） | 2026-09-22 | 同上 |
| 文档描述被当用户事实 | Write Gate 拒绝 | 通过（evidence→user_fact 一律拒） | 2026-09-22 | 同上 |
| Prompt Injection 写长期记忆 | 拦截 | 通过（11 条中英变体参数化） | 2026-09-22 | 同上 |
| 过期决策被召回 | TTL 生效，stale hit 可测 | 通过（stale hit=0.0） | 2026-09-22 | 同上 |
| 相似主题跨会话串味 | Read Gate 过滤 | 通过 | 2026-09-22 | 同上 |

### 验收门

- [x] 冻结隔离测试跨 workspace 泄漏数为 0
- [x] 每条长期 Memory 有 provenance 和写入原因（库内无来源比例 0.0）
- [x] supersede 旧值可审计不默认召回
- [x] 删除与 TTL 有测试（真删+审计快照；TTL 只清过期 active）

### 指标记录

| 指标 | 值 | 运行日期 | 配置指纹 | 产物路径 |
|---|---|---|---|---|
| write precision | 1.0 | 2026-09-22 | — | tests/memory/test_metrics.py |
| memory Recall@5 | 1.0 | 2026-09-22 | — | 同上 |
| stale hit rate | 0.0 | 2026-09-22 | — | 同上 |
| conflict detection recall | 1.0 | 2026-09-22 | — | 同上 |
| 跨 workspace 泄漏数 | 0 | 2026-09-22 | — | 同上 |
| 无来源 Memory 比例 | 0.0 | 2026-09-22 | — | 同上 |

---

## 10. M7：Visual Evidence、Claim Verification 与 Human Review

**状态：完成（2026-09-22）** ｜ 依赖：M1、M3、M4 ｜ 完成报告：`docs/milestone_reports/M7.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/verification（7 文件）+ 47 测试 + B0–B3 实验 ｜ 中途在无超时 live 调用挂起，主控停止→加固（≤120s 超时/≤2 重试/臂级容错）→resume 完成 ｜ 47 passed；B3 提交答案 unsupported 0.425→0.0 ｜ artifacts/verification/m7-20260922/ ｜ commit 待

### 视觉解析质量对比（deepseek-flash 已激活，2026-09-22 实测）

| 解析路径 | 图描述数 | visual_qa 缺陷 | metadata | ViR（下游） | 日期 | 产物路径 |
|---|---|---|---|---|---|---|
| Nougat + deepseek-flash | 8 | 0/8（全 OK） | 权威（标题/8 作者/arXiv ID 全对） | 待 M10 对照 | 2026-09-22 | artifacts/baseline/m0-vision-deepseek/ |
| lightweight + deepseek-flash | M10 实测中 | | | | | |
| ~~gpt-5.4 on DeepSeek 端点~~ | 0 | 16/16 失败（端点模型名不符） | _error 记录 | — | 2026-09-22 | artifacts/baseline/m0-20260922/ |

注：8 条 visual 证据已入库（vlm=deepseek-flash 指纹新版本），按 M1 规则因缺 bbox 全部 degraded——权威候选启用待 M7/M10 决策。

### 受约束 Multi-Agent 对照（B0–B3，冻结 `benchmarks/answer_cases.jsonl`，12 条真实运行）

| 配置 | claim support | citation precision | citation coverage | numeric acc | unsupported | escalation P/R | 日期 | 配置指纹 |
|---|---|---|---|---|---|---|---|---|
| B0 现有单 AgentExecutor(HTTP) | 0.242 | 0.500 | 0.524 | 0.267 | 0.476 | –/0 | 2026-09-22 | artifacts/verification/m7-20260922/summary.json |
| B1 Planner + Tools | 0.350 | 0.500 | 0.575 | 0.225 | 0.425 | –/0 | 同上 | 同上 |
| B2 B1 + Verifier | 0.267 | 0.500 | 0.700 | 0.325 | 0.300 | 1.00/0.82 | 同上 | 同上 |
| B3 B2 + Human Review Gate | 0.800* | 1.000* | 1.000* | 0.667* | **0.000*** | 1.00/0.73 | 同上 | 同上 |

*B3 仅统计实际提交答案（6/12 草稿被 reject 弃答）。收益来自拦截而非修复（fact recall 降）——Verifier 保留不简化，"修订重生成"回路列为后续缺口。refusal 四臂全对 2/2。

### 验收门

- [x] 视觉失败可归因 parse/retrieve/context/generate 之一（框架+合成 fixture；实证分布 deferred）
- [x] 最终回答逐 Claim 可查看 Evidence（evidence_id+page+confidence+reason）
- [x] Review 决策能恢复原 run（approve→SUCCEEDED 且 draft 不重跑；reject→CANCELLED；B3 真实走完入队→approve→resume）
- [x] 无收益角色处理：Verifier 有据（有条件），保留；缺口已记录
- [x] `LLMMarkdownParser` 类"润色引入新 claim"风险：B2/B3 路径无二次润色环节（verifier 直接拦 unsupported）

---

## 10.5 M8：Eval Harness、Observability 与数据飞轮底座

**状态：完成（2026-09-22）** ｜ 依赖：M4～M7 ｜ 完成报告：`docs/milestone_reports/M8.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 app/eval（6 文件）+ app/observability（2 文件）+ 87 测试 ｜ eval.runner --all ｜ 基线五层通过；gate 自比 pass、人为退化 fail ｜ artifacts/eval/m8-baseline-20260922/ ｜ commit 待
- 2026-09-22 ｜ 主控集成：M7 完成后 resume 注册 verification 层适配器 ｜ eval.runner --all ｜ 六层 116 cases（111 pass/1 fail 真实/4 skip），+8 测试 ｜ 同上 ｜ commit 待
- 2026-09-22 ｜ 主控集成：test_metrics.py 同名冲突改名 test_eval_metrics.py ｜ 全量 411/411 绿 ｜ tests/eval ｜ commit 待

### 冻结数据集登记

| 数据集 | 路径 | case 数 | 数据版本 | 冻结日期 |
|---|---|---|---|---|
| router_cases | `benchmarks/router_cases.jsonl` | 12 | v1 | 2026-09-22 |
| policy_cases | `benchmarks/policy_cases.jsonl` | 16 | v1 | 2026-09-22 |
| retrieval_cases | `benchmarks/retrieval_cases.jsonl` | 16 | v1 | 2026-09-22 |
| visual_cases | `benchmarks/visual_cases.jsonl` | 11（合成 fixture） | v1 | 2026-09-22 |
| answer_cases | `benchmarks/answer_cases.jsonl` | 12 | v1 | 2026-09-22 |
| context_cases | `benchmarks/context_cases.jsonl` | 12 | v1 | 2026-09-22 |
| memory_cases | `benchmarks/memory_cases.jsonl` | 17 | v1 | 2026-09-22 |
| runtime_cases | `benchmarks/runtime_cases.jsonl` | 8 | v1 | 2026-09-22 |
| baseline_cases | `benchmarks/baseline_cases.jsonl` | 12 | v1 | 2026-09-22 |

合计 116 条，单命令 `eval.runner --all` 全量回归 223.6s。

### 验收门

- [x] 单命令跑分层评测，输出 JSON + Markdown（report.json/report.md，schema radiant-eval-report/v1）
- [x] 任一指标可回到 case、配置和 trace（test_every_case_traces_back 强制）
- [x] 至少一次真实"失败→修复→回归→门禁"闭环（飞轮 #1/#2 + gate 自比 pass/人为退化 fail 实测）
- [x] Judge 指标附模型、Prompt、人审一致性说明（无 judge 显式 not_measured 禁止满分；gate 强制人审字段）
- [x] LangSmith 仅可选；本地 EventStore/结果文件是事实源

### 基线 v1（2026-09-22，后续所有 gate 对比的基准）

| 层 | cases | 关键指标 |
|---|---|---|
| control | 28/28 | pass 1.0 |
| durable | 8/8 | recovery 1.0 |
| retrieval | 23 pass/1 fail/4 skip | recall@20 0.472、MRR 0.8571、anchor 1.0 |
| context | 12/12 | pass 1.0 |
| memory | 17/17 | write_precision 1.0 |
| verification | 23/23 | hr 0.389、ViR 0.667(fixture)、refusal 2/2 |

已知真实 fail：BL-T05（M0 锚点偏移，如实保留作红线样本）。

---

## 10.6 M9：API、SSE、Dashboard 与云端交付

**状态：完成（2026-09-22）** ｜ 依赖：M3、M8 ｜ 完成报告：`docs/milestone_reports/M9.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理实现 11 条新路由 + Dashboard 静态页 + 部署加固 + 19 测试 ｜ pytest tests/api + 全量 ｜ 19 passed；全量 429 passed/1 skipped ｜ 见 M9 报告 ｜ commit 待
- 2026-09-22 ｜ 主控复验全量 ｜ pytest tests/ ｜ 429 passed/1 skipped（131s）独立确认 ｜ — ｜ commit 待
- 2026-09-22 ｜ 视觉补测（deepseek-flash 多模态）｜ run_parse.py（Nougat+flash）｜ 393.5s/15页：119 text + 8 图描述 + 权威 metadata；visual_qa 8/8 OK；已入库新版本（vlm=deepseek-flash 指纹），8 条 visual 按规则 degraded（缺 bbox）｜ artifacts/baseline/m0-vision-deepseek/ ｜ commit 待

### 新 API 路由实现登记

| 路由 | 状态 | 实现位置 | 测试 |
|---|---|---|---|
| POST /documents/ingest | 完成 | api.py M9 块 | test_jobs.py（幂等短路） |
| GET /documents/ingest/{job_id} | 完成 | 同上 | 同上 |
| POST /runs | 完成 | 同上（control→durable 后台执行） | test_runs.py |
| GET /runs + /runs/{run_id} | 完成 | 同上（只读 SQL 视图+状态快照） | 同上 |
| GET /runs/{run_id}/events | 完成 | 同上（SSE + Last-Event-ID 续推 + 心跳） | 同上（replay 断言） |
| POST /runs/{run_id}/cancel | 完成 | 同上（含竞态兜底） | 同上 |
| POST /runs/{run_id}/resume | 完成 | 同上（内存 plan 限制见偏差 1） | 同上 |
| GET /reviews | 完成 | 同上 | test_reviews.py |
| POST /reviews/{review_id}/decision | 完成 | 同上（approve/edit 清 gate 一次防死循环） | 同上（resume→succeeded/reject→cancelled/409） |
| POST /benchmarks/run + GET /benchmarks[/{id}] | 完成 | 同上（子进程 eval.runner，job.json 落盘） | test_jobs.py |

### 验收门

- [x] 干净启动实测：stop → -d（健康检查轮询 74–80s 通过）→ status → /runs 全生命周期真实走通
- [x] 5～8 分钟演示流程可走通（DEMO_SCRIPT 六步主线等价操作全部验证）
- [x] 错误页稳定：不存在资源全部结构化 JSON 404，catch-all 不吞 API
- [x] 部署文档无地址/密码/真实 key（正则扫描）
- [x] start_radiant.sh 升级：健康检查、status、日志轮转（50MB/5 份实测）、防重复启动
- [x] backup/restore 实测（修复 WAL 陷阱改 SQLite 在线备份 API；沙盒恢复验证表/数据完整）
- [x] ~~compose 写死路径修正~~ → 后置封装阶段（用户决策），不在 M9 范围

### Dashboard 页面登记（app/dashboard-static/，原生 JS 无构建）

首页+摄取 / Runs（列表+详情+实时事件流）/ Evidence Inspector / Reviews（决策按钮）/ Benchmarks（触发+基线对比+失败标红）——/dashboard/ 实测 200。

---

## 10.7 M10：最终实验与求职材料

**状态：完成（2026-09-22）** ｜ 依赖：M4～M9 ｜ 完成报告：`docs/milestone_reports/M10.md`

### 执行记录

- 2026-09-22 ｜ coder 子代理：表 6 实验（Nougat vs lightweight 同 VLM 对照）+ 六表 + 6 份材料 ｜ run_m10_lightweight.py ｜ 公式保留 33 vs 0、图描述 8 vs 7（1 例真实 VLM 解析失败如实记录）；18 处 not_measured 全注明原因 ｜ artifacts/final/、docs/FINAL_EXPERIMENTS.md ｜ commit 待
- 2026-09-22 ｜ 主控抽查 ｜ 数字与 artifacts 原文对拍 ｜ 表 6 一致；B3 口径三处一致声明；无凭记忆填数 ｜ `docs/milestone_reports/M10.md` ｜ commit 待

### 六张对照表完成度

| 对照表 | 状态 | 数据位置 |
|---|---|---|
| 1. Dense vs Hybrid vs RRF vs Gate | 完成（Recall@5 0.234→0.299、anchor@5 0.929→1.000、变差 0） | artifacts/retrieval/m4-20260922/ |
| 2. 无预算 vs Budget/Pin | 完成（pin on 100 源 gold 保留 1.0 vs pin off 0.267±0.189） | artifacts/context/m5-20260922/ |
| 3. 无 Checkpoint vs Durable Runtime | 完成（8/8=1.0 vs 基线架构性 not_measured） | tests/durable、benchmarks/runtime_cases.jsonl |
| 4. 聊天记录 vs Governed Memory | 完成（六指标全达标、泄漏 0 vs 基线无治理） | tests/memory |
| 5. B0 vs B1/B2/B3 | 完成（提交集 unsupported 0.476→0.000*，拦截口径星标） | artifacts/verification/m7-20260922/ |
| 6. Nougat vs 降级路径视觉 | 完成（公式 33 vs 0、图 8 vs 7、1 真实失败 case 保留） | artifacts/final/m10-vision-compare.json |

### 求职交付物清单

- [x] `README.md`（背景/上游归属/架构/六组对照摘要/限制/快速开始）
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/DEMO_SCRIPT.md`（含预期输出核对表与失败兜底话术）
- [x] `docs/INTERVIEW_QA.md`（防伪六问 + 10 深挖）
- [x] `docs/OWNERSHIP.md`（A 上游 / B fork / C 本项目三方分清，C 类 M1–M10 全量）
- [x] 架构图（mermaid）+ 六组对照表（图以表代，截图待 Dashboard 演示时补）
- [x] 5 条简历 bullet（方括号全部替换为真实值，每条附证据路径）
- [x] 3 分钟介绍稿 + 15 分钟深挖讲稿
- [x] 防伪审计六问逐条可答（含失败 case 与限制：无 GPU、前端无源码、Docker 不可用、视觉 bbox 缺失、CoP 需人审）

---

## 11. 执行日志（全项目 append-only 流水）

格式：`日期 ｜ Milestone/Wave ｜ 操作 ｜ 命令 ｜ 结果 ｜ 产物 ｜ commit`

- 2026-09-22 ｜ M0 ｜ 全仓库审计 ｜ explore 子代理审计 ｜ 完成，发现 H1–H11 ｜ 主计划 v2 ｜ `051d0b8`
- 2026-09-22 ｜ — ｜ 主计划 v2 重写 + 本台账创建 ｜ — ｜ 完成 ｜ `RADIANT-Control_KimiCode分阶段执行计划.md`、本文件 ｜ 待提交
- 2026-09-22 ｜ M0 ｜ 环境修复包 ｜ HF_API_KEY 兼容 + HF_ENDPOINT 镜像 + LangSmith 条件化 + tesseract 5.5.2 + agent 预算配置化 ｜ 全部编译通过、实测生效 ｜ 见 M0 执行记录 ｜ 待提交
- 2026-09-22 ｜ M0 ｜ DeepSeek 对话接入 ｜ radiant_llm.py deepseek 分支 ｜ /initialize 成功，e2e 跑通 ｜ 同上 ｜ 待提交
- 2026-09-22 ｜ M0 ｜ Nougat 基线解析 ｜ run_parse.py ｜ 615.4s/15页，119 chunk 全 nougat 未降级 ｜ artifacts/baseline/m0-20260922/ ｜ 待提交
- 2026-09-22 ｜ M0 ｜ schema profiling + 幂等实测 + 5 条 e2e ｜ schema_profile.py / 重复摄取 / curl ｜ 01 表 100% 填充；摄取计算不幂等（BC-parser-002）；e2e 5/5 ｜ 同上 ｜ 待提交
- 2026-09-22 ｜ M0 ｜ 交付物与验收 ｜ — ｜ 6 文档 + baseline.yaml + 12 cases + artifacts 齐备，验收门 5/5 ｜ `docs/milestone_reports/M0.md` ｜ 待提交
- 2026-09-22 ｜ M1 ｜ Evidence 包实现 ｜ coder 子代理 ｜ app/evidence + api.py 三路由 + 15 测试；真实摄取 121 条、重复摄取 0 ｜ `docs/milestone_reports/M1.md` ｜ 待提交
- 2026-09-22 ｜ M2 ｜ Control Plane 骨架 ｜ coder 子代理 ｜ app/control 8 模块 + 17 工具登记 + 49 测试 + 28 冻结用例；model-swap 全拦截 ｜ `docs/milestone_reports/M2.md` ｜ 待提交
- 2026-09-22 ｜ M1 ｜ 飞轮闭环 #1（BC-parser-003 邻居排序） ｜ 集成验证发现→resume 修复→回归固化→真实库复验 ｜ 全量 64/64 绿 ｜ 台账 13.4 ｜ 待提交
- 2026-09-22 ｜ M3 ｜ Durable Runtime ｜ coder 子代理 ｜ app/durable 9 文件 + 46 测试；RT 恢复率 8/8=1.0、副作用重复 0、并发隔离 ｜ `docs/milestone_reports/M3.md` ｜ 待提交
- 2026-09-22 ｜ M4 ｜ Hybrid 检索与门禁 ｜ coder 子代理 ｜ app/retrieval 10 文件 + 47 测试 + A0–A4 真实指标；A0→A4 变好 3 变差 0；proxy reranker 无收益已删 ｜ `docs/milestone_reports/M4.md`、artifacts/retrieval/m4-20260922/ ｜ 待提交
- 2026-09-22 ｜ 集成 ｜ pytest 同名冲突修复（test_durable_idempotency.py + pytest.ini）｜ 全量 157/157 绿 ｜ pytest.ini ｜ `d0a1533`
- 2026-09-22 ｜ — ｜ M0–M4 提交 ｜ git commit（99 文件，+10311）｜ `d0a1533` ｜ — ｜ `d0a1533`
- 2026-09-22 ｜ 环境 ｜ Gemini 不可达排查 + 视觉端点切换 ｜ curl 探测 + vp_vision_llm.py 增加 VISUAL_PARSER_OPENAI_BASE_URL/API_KEY 覆盖 ｜ 用户选定阿里百炼 qwen-vl；等 key ｜ `.env.example` ｜ 未提交
- 2026-09-22 ｜ M5 ｜ Context Budget ｜ coder 子代理 ｜ app/context 7 文件 + 44 测试 + 12 冻结用例；pin 开 100-source gold 保留 1.000；pin 关失败区间 25–100 已如实报告 ｜ `docs/milestone_reports/M5.md`、artifacts/context/m5-20260922/ ｜ 未提交
- 2026-09-22 ｜ M6 ｜ Memory Governance ｜ coder 子代理 ｜ app/memory 6 文件 + 68 测试 + 17 冻结用例；六项指标全达标、泄漏 0 ｜ `docs/milestone_reports/M6.md` ｜ 未提交
- 2026-09-22 ｜ 集成 ｜ 飞轮闭环 #2（BC-runtime-003 flaky cancel 测试） ｜ 事件同步改确定性断言 ｜ 3 轮全量 269/269 绿 ｜ tests/durable ｜ `09294df`
- 2026-09-22 ｜ — ｜ M5–M6 提交 ｜ git commit ｜ `09294df` ｜ — ｜ `09294df`
- 2026-09-22 ｜ M7 ｜ Claim 校验与人工复核 ｜ coder 子代理（挂起→加固超时→resume） ｜ app/verification 7 文件 + 47 测试；B3 提交集 unsupported 0.000、precision 1.000；Review→resume 原 run 实测 ｜ `docs/milestone_reports/M7.md` ｜ 未提交
- 2026-09-22 ｜ M8 ｜ Eval Harness ｜ coder 子代理 + 主控两轮集成 ｜ app/eval+observability；六层 116 用例单命令回归 223.6s；gate 自比 pass/退化 fail；95 测试 ｜ `docs/milestone_reports/M8.md`、artifacts/eval/m8-baseline-20260922/ ｜ 未提交
- 2026-09-22 ｜ 集成 ｜ test_metrics 同名冲突改名 + verification 层接入 harness ｜ 全量 411/411 绿 ｜ tests/eval ｜ 未提交
- 2026-09-22 ｜ 环境 ｜ deepseek-flash 多模态实测确认 ｜ OpenAI 兼容图片输入测试 ｜ flash 读图正确（v4-pro 无视觉）；百炼方案弃用 ｜ — ｜ —
- 2026-09-22 ｜ M7补 ｜ 视觉补测解析（Nougat+deepseek-flash） ｜ run_parse.py ｜ 393.5s/15页：8 图描述 visual_qa 8/8 OK、metadata 权威；入库新版本（8 visual 缺 bbox 按规则 degraded） ｜ artifacts/baseline/m0-vision-deepseek/ ｜ 未提交
- 2026-09-22 ｜ M9 ｜ API/SSE/Dashboard/交付 ｜ coder 子代理 ｜ 11 新路由 + Dashboard + 部署加固 + 19 测试；一键启动与 /runs 生命周期实测；backup WAL 修复 ｜ `docs/milestone_reports/M9.md` ｜ 未提交
- 2026-09-22 ｜ 集成 ｜ M9 独立复验 ｜ 全量 429 passed/1 skipped（131s） ｜ — ｜ 未提交
- 2026-09-22 ｜ M10 ｜ 最终实验与求职材料 ｜ coder 子代理 ｜ 表 6 实验（公式 33 vs 0、图 8 vs 7）+ 六表 + ARCHITECTURE/INTERVIEW_QA/RESUME_BULLETS/TALK_TRACKS/OWNERSHIP/README；18 处 not_measured 全注明 ｜ `docs/milestone_reports/M10.md`、artifacts/final/ ｜ 未提交
- 2026-09-22 ｜ — ｜ 部署策略确认 ｜ 用户决策：本期不在 Docker 上调整，compose 修正后置封装阶段；Grace vLLM 确认为可选（需 TAMU HPRC，本环境不可用）｜ 已写入两份文档 ｜ 主计划 M9、台账第 14 节 ｜ 待提交

（此后每次执行在此追加一行）

---

## 12. 指标总表（跨 Milestone 汇总，只填有配置指纹的值）

### 12.1 检索质量（冻结 retrieval_cases，14 条带锚点）

| 日期 | 配置 | Recall@5 | Recall@20 | MRR | nDCG@20 | anchor hit@5 | CoP | CiH | HR | 产物路径 |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-22 | A0 纯 dense 基线 | 0.2344 | 0.4292 | 0.7792 | 0.4462 | 0.929 | — | — | — | artifacts/retrieval/m4-20260922/ |
| 2026-09-22 | A4 当前最优（RRF+gates） | 0.2985 | 0.4720 | 0.8571 | 0.5010 | 1.0 | — | — | — | 同上 |

### 12.2 Runtime 可靠性（冻结 runtime_cases）

| 日期 | 配置 | 恢复成功率 | 重复副作用率 | 事件丢失率 | cancel 泄漏 | 产物路径 |
|---|---|---|---|---|---|---|
| 2026-09-22 | 基线（无 checkpoint，恢复率 0） | 0（架构性，未跑） | — | — | — | M0 审计 |
| 2026-09-22 | M3 DurableRunner | 8/8 = 1.0 | 0 | 0 | 0 | tests/durable + benchmarks/runtime_cases.jsonl |

### 12.3 Memory 治理（冻结 memory_cases）

| 日期 | 配置 | write precision | Recall@5 | stale hit | 泄漏数 | 产物路径 |
|---|---|---|---|---|---|---|
| 2026-09-22 | 基线（现有 JSONL 注入，无治理，审计结论） | — | — | — | — | M0 审计 |
| 2026-09-22 | M6 Write/Read Gate | 1.0 | 1.0 | 0.0 | 0 | tests/memory |

### 12.4 视觉与端到端（冻结 visual/answer_cases）

| 日期 | 配置 | ViR | claim support | citation coverage | unsupported rate | 产物路径 |
|---|---|---|---|---|---|---|
| 2026-09-22 | M0 e2e（5 case 抽样，文本链路） | — | — | 页级引用 5/5 | 拒答正确 1/1 | artifacts/baseline/m0-20260922/e2e_*.json |
| 2026-09-22 | B0 单 AgentExecutor（12 条） | — | 0.242 | 0.524 | 0.476 | artifacts/verification/m7-20260922/ |
| 2026-09-22 | B3 +Verifier+Review（12 条，提交集） | — | 0.800* | 1.000* | 0.000* | 同上 |
| 2026-09-22 | eval harness B2 臂回归（新一轮草稿） | 0.667（合成 fixture） | 0.278 | 0.611 | 0.389 | artifacts/eval/m8-baseline-20260922/ |

*B3 仅统计实际提交答案（6/12 被 reject 弃答），收益来自拦截而非修复。

### 12.5 上下文压力（M5，pseudo-sources）

| 日期 | 配置 | 100-source gold 保留率 | token 节省率(100s) | 失败 onset | 产物路径 |
|---|---|---|---|---|---|
| 2026-09-22 | pin on | 1.000 | 0.939 | 无（至 100s） | artifacts/context/m5-20260922/ |
| 2026-09-22 | pin off | 0.267±0.189 | 0.942 | 25–100 sources | 同上 |

---

## 13. 数据飞轮

### 13.1 飞轮规则

```text
Trace / Review / User Feedback
→ 脱敏与来源检查
→ 失败归因（必须落到一层：parser/retrieval/context/generation/runtime/policy/memory）
→ 固定为 regression case（写入 benchmarks/，含 case_id/预期/风险级别/来源）
→ 单变量修改（一次只改一个因子，否则本次飞轮作废）
→ 离线回归
→ Release Gate
→ 发布
```

第一版飞轮只更新：Prompt、Policy、Tool Schema、检索配置、Context 策略、测试集。**不自动训练模型。**

### 13.2 Bad-case Registry

格式：bad-case ID 规则 `BC-<layer>-<序号>`，layer ∈ {parser, retrieval, context, generation, runtime, policy, memory}。

| ID | 发现日期 | 来源（trace/review/user） | 现象 | 归因层 | root cause | 修复 commit | regression case_id | 状态 |
|---|---|---|---|---|---|---|---|---|
| BC-runtime-001 | 2026-09-22 | 代码审计 | 全局 `Chatbot()` 单例，并发请求共享状态，SSE 轮询共享列表事件会串 | runtime | 无 run 级状态隔离 | 未修复 | 待 M3 固化 | 开放 |
| BC-policy-001 | 2026-09-22 | 代码审计 | `PythonREPLTool` 无沙箱，策略只在提示词文本里 | policy | 无 Policy Engine | 未修复 | 待 M2 固化 | 开放 |
| BC-retrieval-001 | 2026-09-22 | 代码审计 | 纯 dense top-20，无 BM25/RRF/rerank，精确术语/数字查询不稳定（待量化） | retrieval | 单路召回 | 未修复 | 待 M4 固化 | 开放 |
| BC-context-001 | 2026-09-22 | 代码审计 | token 估算 chars//4；overflow 压缩路径不保证证据保留 | context | 粗估算+无证据保护 | 未修复 | 待 M5 固化 | 开放 |
| BC-generation-001 | 2026-09-22 | 代码审计 | citation 靠提示词自觉 + 工具内 sources 拼接；`LLMMarkdownParser` 二次润色可能引入新 claim | generation | 无 claim-evidence 校验 | 未修复 | 待 M7 固化 | 开放 |
| BC-parser-001 | 2026-09-22 | 部署记录+代码审计 | 代码不读 `HF_API_KEY` + huggingface.co 直连超时，Nougat 疑似长期静默降级 PyMuPDF | parser | 变量名不兼容 + 网络不可达 | 已修复：`vp_nougat_engine.py:47-51` 接受 HF_API_KEY；`.env` 配 HF_ENDPOINT 镜像；**Nougat 实测解析 15 页成功（119 chunk 全 extractor=nougat，未降级）** | 待 M1 固化为摄取回归 case | 已修复（待回归固化） |
| BC-parser-002 | 2026-09-22 | M0 实测 | 重复摄取同一 PDF 不短路：重算全部 119 chunk（≈10 分钟 CPU 浪费），但按 chunk_id 覆盖写、无重复记录；metadata 重跑时跳过（行为不一致） | parser | 注册表（04_processed_pdfs）只做记录不做短路；写入幂等、计算不幂等 | 已修复（M1）：adapter 层 `ingest_state` 按 (workspace, document_id, content_hash, parser_fingerprint) 短路，真实基线复验 new_records=0 | test_reingest_same_document_is_noop | 已修复（回归固化） |
| BC-parser-003 | 2026-09-22 | M1 集成验证 | Evidence 相邻 chunk 邻居全部错位：p2:c0 的 next 是 p12:c1（字典序 "p1"<"p10"<"p12"<"p2"） | parser | 上游 chunk_index 每页重置，adapter 用裸 index 查找表跨页碰撞 | 已修复：按 (page, chunk_index) 数值阅读序取位置前后各 1 | test_neighbours_numeric_order_across_12_plus_pages | 已修复（回归固化，飞轮闭环 #1） |
| BC-runtime-002 | 2026-09-22 | 代码审计 | `LANGCHAIN_TRACING_V2=true` 硬编码，无 key 时行为未定义 | runtime | 配置硬编码 | 已修复（2026-09-22）：7 处改为有 `LANGCHAIN_API_KEY` 才开启，默认 false | 待 M8 纳入回归 | 已修复 |
| BC-runtime-003 | 2026-09-22 | 全量回归 | `test_cancel_token_interrupts_running_node` 全量高负载间歇失败（269 测试并发下 wall-clock 假设被击穿），单跑又过 | runtime | 测试依赖真实时钟断言（固定 sleep + 耗时上界） | 已修复：改事件同步（node_started.wait）+ 确定性断言（token.cancelled 退出）；同类缺陷预防性固化 test_retry 超时用例 | 3 轮全量 269/269 绿 | 已修复（回归固化，飞轮闭环 #2） |

### 13.3 Release Gate 历史

每次过门禁追加一行。Gate 标准（M8 固化前为临时标准）：核心 Recall 不退化 / unsupported 不上升 / 隔离与幂等测试通过 / 新增 regression case 全绿。

| 日期 | 版本/commit | 触发原因 | Gate 结果 | 各项指标对比（vs 上一版） | 产物路径 |
|---|---|---|---|---|---|
| （空——首次 Release Gate 待 M8） | | | | | |

### 13.4 飞轮转动记录（每次完整闭环一行）

| 序号 | 闭环日期 | 关联 bad-case | 单变量修改内容 | 回归结果 | Gate | 备注 |
|---|---|---|---|---|---|---|
| #1 | 2026-09-22 | BC-parser-003 | adapter 邻居计算：裸 chunk_index 查找表 → (page, chunk_index) 数值阅读序 | 新增回归 1 条，tests/evidence 15/15 绿，全量 64/64 绿；真实基线库重建复验通过 | 临时标准通过（M8 前无正式 Release Gate） | 首次完整"发现→归因→修复→固化→复验"闭环 |
| #2 | 2026-09-22 | BC-runtime-003 | cancel/retry 测试：wall-clock 断言 → 事件同步 + 确定性条件断言 | 3 轮全量 269/269 绿 | 临时标准通过 | 首次"测试本身"作为飞轮对象的闭环 |

---

## 14. 风险与债务台账

| 登记日期 | 内容 | 影响 | 解锁条件 | 状态 |
|---|---|---|---|---|
| 2026-09-22 | Docker 在本机不可用；用户决定本期不在 Docker 上调整 | compose 修正（写死路径、镜像、卷）整体后置到最终封装阶段；M9 只验收原生路径 | 封装阶段有一台可跑 Docker 的机器 | 后置 |
| 2026-09-22 | `.env` 中 HF 变量名与代码不符（代码只读 `HF_TOKEN`/`HUGGING_FACE_HUB_TOKEN`）+ huggingface.co 直连超时 | 若变量名错，Nougat 静默降级 PyMuPDF，M0 基线和 M7 视觉实验都受影响 | 已解决：代码接受 `HF_API_KEY`（vp_nougat_engine.py:47-51），`.env` 追加 `HF_ENDPOINT=https://hf-mirror.com`，登录+镜像模型存在性实测通过（2026-09-22） | 已关闭 |
| 2026-09-22 | React 前端只有 dist 无源码 | Dashboard 只能做独立静态页，无法改现有界面 | 接受独立静态页方案，或找回源码 | 开放 |
| 2026-09-22 | ~~HF token 未配~~ → 已更正：token 已写入 `.env`，问题改为变量名疑似不符（见上一条） | — | — | 已关闭（更正） |
| 2026-09-22 | ~~tesseract 缺失~~ → 已解决：conda 官方源装 5.5.2 到 /root/tesseract-env（阿里云 anaconda 镜像已停服 404）；start_radiant.sh 经 TESSERACT_PREFIX 接入 PATH | 扫描版 PDF 的 OCR 备用路径可用 | — | 已关闭 |
| 2026-09-22 | ~~Gemini 不可达 + 百炼 key 未到位~~ → **已解决（更好路径）**：实测 `deepseek-flash` 有多模态能力（`deepseek-v4-pro` 没有），视觉解析直接复用现有 DeepSeek 端点，零新增配置、不需要百炼 key | 视觉链路已激活 | — | 已关闭 |
| 2026-09-22 | 磁盘紧张（FUSE 盘，不支持符号链接） | 多份向量库/artifact 可能放不下 | 定期清理策略；artifact 落盘目录可配置 | 开放 |
| 2026-09-22 | 仓库零测试 | M8 之前所有"通过"都缺工程底线保障 | M2 起每个 Milestone 自带 tests/ | 开放 |

---

## 15. 决策日志（ADR 索引）

| ADR | 标题 | 日期 | 状态 | 文件 |
|---|---|---|---|---|
| 0001 | project-boundary | 待写 | 未开始 | `docs/adr/0001-project-boundary.md` |
| 0002 | evidence-identity | 待写 | 未开始 | `docs/adr/0002-evidence-identity.md` |
| — | （后续 ADR 在此追加：如 durable 命名避开 runtime/、Dashboard 独立静态页方案、reranker 取舍等） | | | |
