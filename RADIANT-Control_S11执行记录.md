# RADIANT-Control S11 执行记录（2026-09-29）

> S11：Release Gate 治理与受控链聊天前端。承接 S10 收口（诚实口径）之后，
> 解决「红灯到底是回归还是尺子问题」，并交付真正调用受控链的用户聊天界面。

## 1. 修改前问题

- S10 实跑 Release Gate 两条红灯，性质不明：retrieval.recall@20 −0.000991、verification.hr 0.389→0.458。
- 报告缺少可比性元数据：无 metric_schema_version / evaluator_version / 语料内容哈希，Gate 无法识别「两次运行用了不同语料/不同评估器」。
- hr 指标语义混淆：B2 臂无条件提交 draft，review 答案混入；M8 与 S10 的 claims.py 版本不同、draft 是实时 LLM 采样。
- 只有运维 Dashboard 和旧链编译前端，没有面向用户的受控链聊天界面。

## 2. 根因（T1 诊断结论，证据见 docs/learning/S11_A/B/C_*.md）

- **recall 红灯 = 语料版本漂移**：M8 基线跑在 v1 摄取上，S10 跑在 v3 摄取上（同一 PDF 重解析，evidence_id 全变）。检索代码全链路确定性（BM25/RRF/rerank/gate 均有稳定二级排序键），不是代码回归。
- **hr 红灯 = 指标语义 + 评估器版本 + 采样三重不可比**：hr 测的是「生成器幻觉（verifier 旁观）」，不是「verifier 变严」；claims.py 切句规则在 8490cf2 变更；同代码两次运行 hr 相差 0.07。
- **BL-T05 = 稳定的真实召回缺陷**：gold page-6 的 13 条 chunk 存在且可索引，但两路召回都不召回（三次独立运行一致复现）。保留 fail，不删不改。

## 3. 修改文件

| 阶段 | 文件 |
|---|---|
| S11-A | `app/eval/versions.py`（新）、`app/eval/fingerprint.py`、`app/eval/runner.py`、`app/eval/release_gate.py`、`tests/eval/test_baseline_compatibility.py`（新）、`tests/eval/test_release_gate.py` |
| S11-B | `app/retrieval/pipeline.py`（全阶段 Top-K trace）、`tests/retrieval/test_repeatability.py`（新） |
| S11-C | `app/verification/experiment.py`（gate-time 语义字段）、`app/eval/adapters.py`（`_aggregate_answer_metrics`）、`app/eval/release_gate.py`（Gate 改观察 final_committed）、`tests/eval/test_verification_metrics.py`（新）、`tests/eval/test_verification_adapter.py` |
| S11-D | `deploy/run_release_gate.sh`（RADIANT_GATE_BASELINE 覆盖） |
| S11-E | `app/control-chat-static/`（index.html/app.js/style.css，新）、`app/api.py`（挂载）、`app/dashboard-static/index.html`（导航入口）、`tests/api/test_control_chat.py`（新） |
| S11-F | `deploy/smoke_e2e.sh`（+3 项 control-chat 检查）、`README.md`、本文档 |

## 4. 测试结果

- 全量：`runtime/bin/python -m pytest -q`（裸命令）→ **596 passed, 0 failed, 0 error**（S10 基线 567 + 新增 29）。
- 分阶段：tests/eval 111 → 121；tests/retrieval 47 → 50（新增重复性 3 项）；tests/eval+verification 165；tests/api +7（control-chat 契约）。
- 修复过程发现：repeatability 测试因 HF 直连挂起——钉死 `HF_HUB_OFFLINE=1` + 镜像后 39s 通过。

## 5. Gate 结果（S11-D/F 实跑）

- 新基线：`artifacts/eval/s11-baseline-20260929/`（含 `baseline_manifest.json`：来源 run、git commit b53030b、metric_schema/evaluator/dataset/配置指纹、已知失败、批准原因、不作质量提升证明的约束）。
- candidate-1（`artifacts/eval/s11-candidate-1/gate.json`）：**pass**（17 pass / 0 fail / 1 skip；baseline_compatibility pass）。
- candidate-2（`artifacts/eval/s11-candidate-2/gate.json`）：**pass**（17 pass / 0 fail / 1 skip）。
- 两次独立运行均：无 baseline_incompatible、retrieval 核心指标逐位一致（recall@20 0.426467 = 0.426467）、final_committed_unsupported_rate 未回退（0.25→0.20→0.00）、durable/memory integrity 全过、review 单列。
- 对照：S10 报告 vs M8 基线现在正确输出 `baseline_incompatible`（旧比较口径退役），历史产物未覆盖。

## 6. 新前端

- 地址：`http://127.0.0.1:8080/control-chat/`（`/control-chat` 自动 307 跳转；Dashboard 导航有「受控聊天」入口）。
- 只走受控链：`POST /runs` → SSE 事件时间线 → 快照渲染（五节点状态、verify_action、逐 claim verdict、证据引用弹层、cancel/resume、waiting_review 跳审核队列、历史 run 回放）。
- 路由拒绝（run_id=null）明示展示，无 /stream-query、无旧链回退；全文本 DOM API 渲染（XSS 安全）。

## 7. 已知限制

- BL-T05 仍 fail（真实召回缺陷，证据已记录，未修复）。
- agent_e2e accept_rate 在 0~1/6 间随 LLM 采样波动（candidate-1: 1/6，candidate-2: 0/6），视觉回答质量未达稳定自动 accept——不作「视觉问答正确」宣称。
- verification 的 LLM draft 采样使 detected/hr 类指标跨次波动（±0.1 量级）；Gate 已改用 committed 口径，但小样本（12 case）下 committed 指标仍有采样噪声。
- 前端未经真实浏览器人工 smoke（EventSource 重连等行为以契约测试为准）。
- `app/api.py` 1967 行单文件集中的结构债务仍在。

## 8. 简历可用 / 不可用

**可写（有 artifact 背书）：**
- 评测门禁具备跨运行可比性治理：metric schema/evaluator 版本 + 语料内容哈希指纹，不兼容基线自动判 baseline_incompatible（fail-closed），实证检索层跨独立运行逐位可复现（recall@20 三次一致）。
- 指标语义治理：区分 verifier 检测风险（detected）与最终提交答案质量（final_committed），review 永不计 pass；S11 基线两次独立 candidate 全过。
- 受控链聊天前端：/runs + SSE 实时链路与 claim 级引用核验，XSS 安全渲染，契约测试 7 项。

**不可写：**
- 「视觉问答自动正确」「E2E 质量 6/6」——accept_rate 0~1/6。
- 「BL-T05 已修复」——仍 fail。
- 「Release Gate 全绿证明质量提升」——S11 基线只锚定同口径回归，manifest 明确不证明提升。
