# ADR 0001：项目边界

- 日期：2026-09-22
- 状态：已接受
- 背景：在 fork 的 RADIANT-LLM（上游 `zev94/radiant-llm` 镜像解包）之上做 RADIANT-Control 改造，需要固定"做什么、不做什么"，防止范围漂移。

## 决定

### 做

以公开技术 PDF 为数据源，保留上游解析管线（Nougat + VLM 图表描述 + PyMuPDF 降级），新建 Agent Control Plane：intent router、结构化 plan、schema guard、policy engine、budget controller、durable runtime（checkpoint/retry/幂等）、hybrid 检索（BM25+dense+RRF+rerank+gate）、上下文预算与证据保留、Memory 读写治理、claim-evidence 校验与人工复核、eval harness 与 release gate、FastAPI/SSE/Dashboard 与原生一键部署。

### 不做

- 不做 γ 能谱分析；不需要能谱图、核素库、衰变数据库或 MATLAB 算法；
- 不要求建立领域知识图谱；
- 不依赖真实企业内部数据；
- 第一版不做模型训练或自动微调；
- 第一版不追求大量 Agent 角色；
- 不把会话 JSONL 冒充长期 Memory；
- 不把视觉描述文本化后宣称"解决了多模态理解"；
- 不重写前端（React 源码不在仓库，Dashboard 走独立静态页）；
- 本期不在 Docker 上做任何调整（本机 Docker 不可用；compose 修正后置封装阶段）；
- 不重构 `app/` 单体布局为 `src/`（新增模块按主计划第 4 节映射落位）。

### 模型与供应商边界（2026-09-22 用户决策）

- 对话/Agent：DeepSeek（OpenAI 兼容端点，`deepseek-v4-pro` / `deepseek-flash`）；
- 视觉解析（VLM）：Gemini（`gemini-2.5-flash` 起）；
- Embedding：本地 `BAAI/bge-base-en-v1.5`（CPU）；
- 不做"降级凑活"：视觉链路不可用就停，不用无视觉模型冒充 VLM 产出。

## 后果

- 所有新指标自测自证，论文数字仅作背景引用；
- 模型供应商可替换性（model-agnostic）成为架构约束：Control Plane 不绑定任何单一 provider；
- 混合 provider（DeepSeek+Gemini+本地 embedding）本身就是论文"model-agnostic"宣称的实证扩展。
