# 上游审计：RADIANT-LLM（zev94/radiant-llm 镜像）

> M0 交付物 ｜ 2026-09-22 ｜ 区分【论文声明】【README/镜像声明】【代码审计发现】【尚未验证】

## 1. 上游身份

| 项 | 值 | 类别 |
|---|---|---|
| 论文 | RADIANT-LLM: an Agentic RAG Framework for Reliable Decision Support in Safety-Critical Nuclear Engineering（arXiv:2604.22755v1, 2026-03-04，Texas A&M） | 论文声明 |
| 代码载体 | Docker 镜像 `zev94/radiant-llm:latest`（Docker Hub） | 镜像声明 |
| 获取方式 | 经镜像站 `docker.1ms.run` 逐层拉取 + SHA256 校验后解包（本机 Docker 不可用），见 `部署记录-无Docker运行.md` | 代码审计发现 |
| 公开 git 仓库 | 论文 §3.6 称"verbatim questions/rubrics/YAML 将发布于公开仓库"——**未获得**，评测材料不在镜像内 | 尚未验证 |
| 镜像内代码许可 | 镜像未附带独立 LICENSE 文件；论文为学术出版物 | 尚未验证（见 OWNERSHIP.md） |

## 2. 镜像内容清单（解包后）

- `/usr/local` → 本仓库 `runtime/`（Python 3.12.13 自包含解释器 + 全部依赖）
- 应用代码 → 本仓库 `app/`：FastAPI 后端（`api.py`）、核心逻辑（`radiant_llm.py`）、13 个工具（`tools/`）、解析/检索/会话/技能（`utils/`）、React 打包产物（`frontend-dist/`，**无源码**）、系统提示词（`system_prompt_radiant_llm.yml`）、技能库（`radiant_llm_skills/`）
- 部署定义 → `Docker_Executable/`（compose、.env 模板、挂载点）

## 3. 关键依赖版本（镜像锁定）

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.13 | 运行时 |
| FastAPI / uvicorn | 0.115 | API 层 |
| LangChain 全家桶 | 0.3.13 | agent/tools/RAG |
| ChromaDB（langchain-chroma） | — | 向量库 |
| transformers（nougat-small 直调） | — | 文本/公式/表格解析 |
| PyMuPDF (fitz) | — | 轻量解析降级路径 |
| Dash 2.18 | — | **已废弃**的旧 UI（`radiant_llm.py:585-586` 直接 raise） |
| pytesseract | 包有二进制无 | OCR 备用（本机已由 conda 补 tesseract 5.5.2） |

完整清单见 `app/requirements.txt`（镜像环境锁定文件）。注意：requirements 中的 opentelemetry 全家桶（:103-112）在代码中**零引用**，是镜像死依赖。

## 4. 与论文的方法学偏差（代码审计发现）

1. 论文称"semantic chunking"，代码是定长 500 字符/100 重叠分块（`vp_config.py:75-78`）。
2. 论文式 (8) 的视觉证据缺失强制拒答，代码中只有提示词文字，无代码强制。
3. 论文 Table 1 的 8 工具与代码 13 工具不完全对应（见 BASELINE.md §2.4）。
4. Nougat 失败静默降级 PyMuPDF，论文未提及降级路径（`vp_pipeline.py:189-249`）。
5. 论文未提及 Dash→React 前端迁移（镜像内 Dash 已废弃）。

## 5. 镜像内不包含（不可声称复现论文）

- OSTI 1000 篇元数据 CSV 与 250 篇 3S 语料；
- 页级 benchmark 的 3 个页面、15 个问题、评分 YAML；
- UNFSF benchmark 12 问与评分；
- 任何预建知识库（`Docker_Executable/data/` 为空）；
- 前端源码、OSTI 批量下载脚本。

## 6. 尚未验证

- 镜像 `zev94/radiant-llm` 与论文作者团队的官方关系（Docker Hub 账号归属未核实）；
- 镜像是否被篡改（逐层 SHA256 校验的是"镜像站副本与 Docker Hub 一致"，不是"与作者发布一致"）；
- `build/`、`radiant_llm.egg-info/` 与安装态的一致性。
