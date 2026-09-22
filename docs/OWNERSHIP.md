# OWNERSHIP：代码归属与贡献边界

> M0 交付物 ｜ 2026-09-22 ｜ 本文件必须在每次发布/简历使用前复核更新

## 三方归属

### A. 上游（论文作者团队 / zev94 镜像）

- `app/` 全部原始代码：FastAPI 后端、LangChain agent 主链路、13 个工具、Visual-Parser 解析管线（vp_*）、Chroma 检索、会话管理、Grace vLLM 客户端、React 前端产物；
- `runtime/` 解释器与依赖；
- `Docker_Executable/` 部署定义；
- 论文（`paper.md`）与全部论文实验数字。

许可状态：镜像内**无独立 LICENSE**。在作者明确许可前，本项目仓库不重新分发镜像本体，仅以"解包自用+增量改造"方式使用；简历与对外材料中明确标注上游来源与论文引用（arXiv:2604.22755）。

### B. fork 既有增量（仓库所有者，RADIANT-Control 计划之前）

| 内容 | 位置 | 说明 |
|---|---|---|
| 领域技能库 | `app/radiant_llm_skills/` | Tier-0 策略 + 6 个 Tier-1 包、hybrid 路由、两级注入扫描 |
| MANIFEST 签名 | `maintenance_scripts/generate_skill_manifest.py`、`app/utils/skill_manifest.py` | SHA-256 完整性校验 |
| 无 Docker 原生运行 | `start_radiant.sh`、`runtime/`、`部署记录-无Docker运行.md` | 镜像层解包 + 校验 + 原生启动 |
| 混合模型接入（DeepSeek 对话 + 本地 embedding） | `app/radiant_llm.py` deepseek 分支 | 2026-09-22 M0 新增 |

### C. RADIANT-Control 增量（本项目核心贡献，M0 起）

| 内容 | 位置 | 状态 |
|---|---|---|
| 环境修复：HF 变量名兼容、HF 镜像、LangSmith 条件化、tesseract 接入、agent 预算配置化 | `vp_nougat_engine.py`、`radiant_llm.py`、`tools/*.py`、`utils/general_utilities.py`、`start_radiant.sh` | M0 已完成 |
| 基线分析书 / 上游审计 / 本文件 / ADR | `docs/` | M0 已完成 |
| 冻结 benchmark 与评测产物 | `benchmarks/`、`artifacts/` | M0 起步 |
| Control Plane / Durable Runtime / Hybrid 检索 / Memory 治理 / Claim 校验 / Eval Harness | `app/control/`、`app/durable/`、`app/retrieval/`、`app/memory/`、`app/evidence/`、`app/eval/` | M1–M8 待建 |

## 使用规则

1. 简历与 README 中的每一条能力声明，必须能归入 B 或 C 并给出 commit/文件证据；A 类能力只能表述为"基于上游 RADIANT-LLM"。
2. 论文实验数字（CoP/CiP/CiH/HR/ViR 任何值）永远是 A 类，不得引用为本项目结果。
3. 对上游代码的任何修改（包括 M0 环境修复），在 commit message 中注明 `[upstream-mod]`，便于将来生成纯净 diff。
