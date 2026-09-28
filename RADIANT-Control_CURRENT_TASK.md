# RADIANT-Control Current Task

- updated_at: 2026-09-24
- plan_version: V3
- stage: S9
- track: T2
- task_id: S9-8
- status: DONE
- last_verified_commit: `994db75`
- dirty_worktree_acknowledged: true

## 已验证完成

**V3 双轨计划 S0～S9 全部完成，最终门通过（2026-09-24）。**

- S9 全部 8 项：/query 统一（默认控制链+显式 legacy 开关+typed error 不落旧链）；.gitignore 治理（未删用户文件）；core dump 诊断（退出期 C++ 线程拆除竞争，低频，未宣称修复，`artifacts/control-v3/S9/abort-ab.txt`）；日志轮转+GET /health/ready；一键 smoke `deploy/smoke_e2e.sh` 实测 10/10 OK；`docs/ARCHITECTURE_S9.md`；`docs/RESUME_MATERIALS_S9.md`。
- 全量回归：**557 passed, 0 failed**。
- 交付物清单：docs/learning/ S1–S9 九份讲义；docs/adr/ 0001–0004；artifacts/control-v3/S1～S9 九份阶段报告与证据包；bad-cases.jsonl；eval/s8-agent-e2e 报告（六段指纹）；简历材料。

## 下一条唯一动作

无。计划完成。后续可选（需用户决策，非本计划范围）：
1. `git commit` 分阶段验收（本会话严格遵守了不自动 commit/push；工作区含大量未提交改动与未跟踪产物，建议用户审阅 diff 后分批提交）；
2. 扩大语料（多 PDF 摄取）后重跑 S3/S6/S7 指标；
3. 生产实例 4915 重启以加载全部新代码（当前运行的是 09-23 旧代码）。

## 禁止重复/禁止进入

- 禁止重做 S0～S9 任何子任务。
