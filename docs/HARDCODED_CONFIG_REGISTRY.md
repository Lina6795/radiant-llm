# 写死配置登记表（M0 任务 7）

> 2026-09-22 ｜ 原则：只登记和配置化，不改行为。Docker 相关只登记不处理（后置封装阶段）。

## 已配置化（M0 完成）

| 位置 | 原写死值 | 现配置项 | 默认值（行为不变） |
|---|---|---|---|
| `app/radiant_llm.py:590` | `AGENT_MAX_ITERATIONS = 40` | `RADIANT_AGENT_MAX_ITERATIONS` | 40 |
| `app/radiant_llm.py:591` | `AGENT_MAX_EXECUTION_TIME_SECONDS = 600` | `RADIANT_AGENT_MAX_EXECUTION_TIME_SECONDS` | 600 |
| `app/radiant_llm.py:592` | `AGENT_ITERATION_WARNING_MARGIN = 5` | `RADIANT_AGENT_ITERATION_WARNING_MARGIN` | 5 |
| `app/radiant_llm.py:180` 等 7 处 | `LANGCHAIN_TRACING_V2=true` 硬编码 | 有 `LANGCHAIN_API_KEY` 才开启，否则 false | 默认关闭 |
| `start_radiant.sh` | 无 tesseract | `TESSERACT_PREFIX` | `/root/tesseract-env` |
| `Docker_Executable/.env` | 无 HF 镜像 | `HF_ENDPOINT` | `https://hf-mirror.com` |

## 只登记、不处理

| 位置 | 写死内容 | 风险 | 处理计划 |
|---|---|---|---|
| `Docker_Executable/docker-compose.yml:44,47` | `/mnt/lina/RADIANT_LLM/...` 卷路径 | 换机器即坏 | 封装阶段改 `${DATA_ROOT}` |
| `app/api.py:500` | `/radiant-llm/frontend-dist` Docker 回退路径 | 候选列表第 3 位，前面命中即不触发 | 封装阶段 |
| `app/radiant_llm.py:510` | `/radiant-llm` Docker 配置回退路径 | 同上 | 封装阶段 |
| `app/radiant_llm.py:1294-1312` | 支持的模型清单硬编码 | 加模型要改代码 | M2 登记 ToolSpec/模型注册表时统一处理 |
| `app/radiant_llm.py:2255-2271` | Dash 下拉模型选项（已废弃 UI） | 死代码 | 随 Dash 清理 |
| `app/utils/vp_config.py:87,95` | 默认视觉模型 `gpt-5.4` / `gemini-3-pro-preview` | 与可用 provider 不匹配时 400 | M1 adapter 配置化 |
| React `frontend-dist/` bundle | 模型下拉选项硬编译 | 新模型无法在下拉框出现（API 直调不受影响） | M9 Dashboard 独立静态页解决 |

## 新增环境变量一览（M0）

`RADIANT_AGENT_MAX_ITERATIONS` / `RADIANT_AGENT_MAX_EXECUTION_TIME_SECONDS` / `RADIANT_AGENT_ITERATION_WARNING_MARGIN` / `TESSERACT_PREFIX` / `HF_ENDPOINT`（另：`OPENAI_BASE_URL` 为 DeepSeek 端点，用户已配）
