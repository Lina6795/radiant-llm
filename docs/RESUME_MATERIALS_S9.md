# S9-8 求职交付材料（2026-09-24 初稿；2026-09-28 S10 Release Audit 修订口径）

## 简历 4～5 行

- 在 RADIANT-LLM（核工程 PDF Agent）上落地了一条**可计划、可校验、可恢复的受控 Agent 主链**：确定性中英文意图路由 → 结构化五步计划（search→inspect→context→draft→verify）→ Schema/Policy 前置校验（非法计划工具调用 0 次）→ Durable 执行（checkpoint/fencing/typed retry/租约续约），步骤间数据用结构化输出引用绑定（禁模板 eval），全链 567 项测试以裸 `python -m pytest -q` 独立通过（不依赖任何存活服务进程）。
- 实现 **BM25+Dense+RRF 混合检索与 metadata/相关性/充分性门**：修复了版本化证据错版命中缺陷（8/8 冻结 case 旧版命中→0），四配置对照 hybrid 为唯一 hit@5 8/8 配置；诚实决策不采用 Cross-Encoder（无模型+离线，ADR 含复评条件）。
- 实现**预算化上下文与可治理 Memory**：ContextPackage 分区预算+来源配额+锚点保护+去重+邻居扩展（无预算 trace 不调模型）；Write Gate 默认拒绝（write_precision 1.0，含 secret/injection/evidence-as-fact 攻击电池全拒）、Read Gate 跨 workspace 泄漏 0（口径：workspace 逻辑隔离，非用户身份认证）；写入路径为显式 `POST /memories` + 回答被 accept 后自动写 session 摘要/证据指针（user_fact/decision 仍需人工确认，禁止自动提升）。
- 实现**有界 Draft→Claim→Verify→Revise/Review 回答验证**：确定性 atomic claim 抽取与 Claim-Evidence Map，每条 claim 产出 supported/unsupported/conflict/out-of-scope verdict，反思修订最多一次；verify 前预暂停路径支持 approve/reject 恢复原 run，verify 后 review 结局进人工审核队列留审计轨迹。
- 实现**真实视觉证据控制与可回归评测飞轮**：figure-level 视觉证据（无虚构 bbox）进入混合检索与 Visual Fact Gate（视觉主张只认视觉证据）；Harness 驱动真实主链 E2E（判定口径 accept/review 分列，review 不计 pass，逐 case 落 trace 与配置指纹），统一六段指纹（git/env/data/model/deps/run），fail-closed Release Gate 接入发布路径并实跑留档，bad case 闭环登记。

## 面试 Q&A（要点）

- Q：步骤间数据怎么传递？A：结构化输出引用（ADR-0003）：arguments 内嵌受控值类型，Guard 静态校验来源 step 存在、依赖闭包、path 语法、类型相容；Runner 从 succeeded checkpoint 解析，resolved 参数重过目标工具 schema，失败 typed error 且工具调用 0 次；禁字符串模板 eval。
- Q：retry 和 resume 的区别？A：retry 只重试明确 retryable error（同进程、指数退避、分类留痕）；resume 从 SQLite 重建整个 run（plan/workspace 持久化于 run_plans 表），succeeded checkpoint 命中即跳过，崩溃恢复后成功 step 重复执行数 0。
- Q：怎么防止模型编答案？A：三层——ContextPackage 前置（无预算 trace 不调模型）；Verifier 逐 claim 对真实证据判定（数字/单位/限定词严检）；review 结局进人工审核队列而非硬答。
- Q： Lease/fencing 实际拦过什么？A：修过一个真 bug：租约 TTL 30s 无续约，>30s 的 run 会自围栏永久卡死（py-spy+lease 表归因），修复为过半 TTL 续约；旧 owner 被接管后写 checkpoint/event 数为 0（fencing 测试实证）。
- Q：检索质量怎么证明？A：冻结 8 case 对照（keyword/BM25/Dense/Hybrid 同语料同 top-k），hybrid MRR .646 且 hit@5 8/8；视觉 5 case：text-only 召回 0.0 vs hybrid 1.0。单测通过率不等于检索质量，指标只来自真实语料冻结集。

## 限制清单（诚实）

- 语料规模：单 PDF（attention 论文）基线；指标为小样本行为验证，不作泛化质量宣称。
- 视觉证据全部 figure-level（无 bbox），未宣称 region-level；VLM 描述不等于视觉事实证明（Visual Fact Gate 已强制）。
- 确定性 Verifier 偏严格：真实 LLM 回答常升级人工 review，这是设计语义不是错误；judge 与人工一致性样本仅 1（agreement=false 已记录）。
- E2E 质量口径（S10 修订）：6 个 E2E case 的 Agent 链全部执行成功，但 verify 多数升级 review（S10 实跑：accept 1/6、review 5/6、supported_claim_rate 0.375）；S10 起 review 不再计入 pass——"回答质量好"不作宣称。
- Release Gate 当前候选实跑记红（`artifacts/eval/s10-20260928-200710/gate.json`）：recall@20 漂移 −0.001（单负例波动）与 verification.hr 上升（更严验证器的真实效应）触发 fail-closed；两条均待后续阶段处置，不放宽阈值。
- 未实现 Cross-Encoder（无模型+离线）、未接入 LangGraph（自研 StateGraph，诚实命名）、旧链 /stream-query 仍保留为 legacy 流式面。
- 退出期 C 级 abort（S9-4 诊断中，见下节）偶发；不影响已完成的测试结论。

## S9-4 core dump 诊断结论（先诊断后修）

- 两个 core（core 1.5G / app/core 1.4G）均为 `runtime/bin/python`，终止于 SIGABRT；
  core 内含 CPython `FATAL: exception not rethrown`，进程 stderr 为
  `terminate called without an active exception`；加载库含 torch/OpenBLAS/gfortran。
- 单因素排查（torch/sentence_transformers/chromadb/radiant_llm/api 导入退出、
  BGE 后台线程退出）均未复现；abort 只在完整 pytest+TestClient+SSE+混合检索
  场景退出期出现 → 判定为**解释器退出期 C++ 线程拆除与在途异常竞争**
  （OpenBLAS/tokenizers 类工作线程在 Python fatal error 路径中被 terminate）。
- 缓解验证：`OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1` 对照实验（结果见
  artifacts/control-v3/S9/abort-ab.txt）；生产建议随服务 env 设置单线程 BLAS。
- core 文件未删除（规则），由 S9-3 gitignore 排除入库。
