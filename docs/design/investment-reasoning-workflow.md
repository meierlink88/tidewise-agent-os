# Investment Reasoning Workflow

## 目标

`investment-reasoning` 是一条由 Schedule 触发的固定分层投研推理链。它读取指定时间窗内已经完成 Event 分析的 Event、Variable Signal Fact 和标准锚点，从地缘政治开始，依次推导宏观经济与产业链影响，最终给出产业链及其标准节点的升温、降温、分化、无显著变化或证据不足结论。

Event 进入 Graphiti 前后的分类、锚点匹配和直接 Signal Fact 构建属于 `event-extraction`。本 Workflow 不重新提取 Event，不修改 Event、Fact 或 Signal，也不把未审核的中间推导写回 Graphiti。Agno PostgreSQL 保存 Workflow 运行状态和各 Step 结果；完成审核的产品结论独立保存为 Investment Conclusion Artifact。

## 冻结边界

- Schedule Payload 是投研命题和 Event 回看时长的唯一运行输入。
- 核心 Workflow 不使用 Planner Agent；`prepare-investment-context` 直接解析 Schedule 输入并冻结 `decision_at`。
- 当前只执行地缘政治、宏观经济和产业链三层；Company 层暂不执行，也不得在结果中生成 Company 结论。
- `capabilities/investment/` 持有投研合同、分层检索政策、根谱门禁、传导和结论规则。
- `agents/investment_reasoner.py` 负责受限语义判断，`agents/investment_reviewer.py` 负责最终语义审核。
- `workflows/investment_reasoning.py` 只组装固定 Step、生命周期和 Studio seed。
- `sematica/graphiti/investment.py` 只封装 Graphiti 原生检索与 group/time/ID/拓扑精确读取，不持有投研业务规则。

## Schedule 输入

推荐 Schedule 的 `payload.message` 使用 JSON 字符串：

```json
{
  "question": "分析最近事件对地缘政治、宏观经济和产业链节点投资价值的影响",
  "event_window_hours": 48,
  "include_company": false
}
```

Agno 的 HTTP 层在配置 Workflow `input_schema` 时会先把原始 message 当 JSON 解码，因此本 Workflow 不在 Agno 入口声明 `input_schema`，而由第一个确定性 Function 把原始自然语言或 JSON 统一校验为 `InvestmentReasoningInput`。该合同必须拒绝未知字段和 `include_company=true`。为迁移现有本地 Schedule，可在一个兼容周期内接受包含明确“最近 N 小时”的旧中文 message；不得静默使用另一个隐藏时间窗。`decision_at`、产业链上限、检索预算和最多三跳由确定性代码控制，不接受 Schedule 覆盖。

## 固定五步

```text
Schedule Payload
        ↓
prepare-investment-context
        ↓
analyze-geopolitical-impact
        ↓
analyze-macro-impact
        ↓
analyze-industry-impact
        ↓
review-and-finalize
```

### 1. prepare-investment-context

确定性 Function，直接读取 `step_input.input`，不调用模型。

它只冻结所有层共享的基础上下文：

- `decision_at` 与 Event 时间窗；
- 时间窗内全部 Event 快照；
- 这些 Event 关联且在 `decision_at` 有效的 Signal Fact；
- Event 已关联的标准锚点索引；
- Graphiti group、数据版本、检索预算和执行上限。

它不得加载候选产业链的全量节点或上下游拓扑。产业链拓扑只能在 `analyze-industry-impact` 确认候选产业链后按需加载。

### 2. analyze-geopolitical-impact

输入共享基础上下文，在已初始化且有效的 `GeopoliticRivalry` 标准锚点范围内分析。

内部顺序是：

1. 使用 Event、直接 Signal 和标准锚点执行 Graphiti 原生召回；
2. 加载命中的地缘政治锚点和当前有效普通 Fact；
3. Reasoner 形成初步判断；
4. 如确有缺口，只允许提交有界补充查询，由代码执行 Graphiti 检索；
5. 形成地缘政治层结论并通过确定性根谱门禁。

第一层没有上游结论。方向性地缘政治判断必须具有当前有效的直接 Signal 根；只有 Event、MENTIONS 或普通 Fact 时只能输出 `INSUFFICIENT_EVIDENCE`，不能强行给出方向。

### 3. analyze-macro-impact

输入必须包含完整、已校验的地缘政治层结果，并在已初始化的 `MacroEconomic` 标准锚点范围内分析：

```text
地缘政治层结论
+ 本时间窗宏观经济直接 Signal
+ 宏观政策锚点与当前有效机制 Fact
→ 宏观经济层结论
```

即使没有宏观经济直接 Signal，本 Step 仍必须执行。只有当有效地缘政治结论和明确机制 Fact 同时存在时，才可形成跨层派生结论；派生结论必须继承上层 Event/Signal 根，记录父结论与机制 Fact，并降低或保持置信度。缺少任一项时输出 `NO_MATERIAL_CHANGE` 或 `INSUFFICIENT_EVIDENCE`，不得发明传导机制。

### 4. analyze-industry-impact

输入必须同时包含地缘政治层和宏观经济层结果，以及产业链节点直接 Signal。产业链是节点集合与分析视图，不拥有直接 Variable Signal。

该 Step 才执行产业链检索与拓扑加载：

1. 根据直接节点 Signal 所属真实产业链、上层结论及机制 Fact 召回候选 `IndustryChain`；
2. 每条候选链独立加载全部标准 `ChainNode` 和真实拓扑边；
3. 找到直接 Signal 根节点，或通过“上层结论 + 机制 Fact”建立受控节点落点；
4. 沿真实拓扑最多执行三轮传导；
5. 覆盖该链每个标准节点，分别生成短、中、长期趋势；
6. 汇总产业链整体升温、降温、分化、无显著变化或证据不足。

语义相关只能用于候选召回，不能单独启动方向判断。产业链结论必须由其真实成员节点的直接或受控传导结果汇总，不得使用链级 Signal 启动方向。交叉出现在多条产业链中的节点必须按 `(industry_chain_id, chain_node_id)` 分析，禁止跨链污染。

### 5. review-and-finalize

确定性门禁先检查全部层级和产业链节点结论，Reviewer 再做语义审核。该 Step 不创造新的因果方向，只负责审核、一次有界修正和安全降级。

最终结果包括：

- 一句话投研结论；
- 地缘政治、宏观经济和产业链三个层级的结论；
- 每条产业链及全部节点的短、中、长期趋势；
- Event → Signal Fact → 上层结论 → 机制 Fact/拓扑边 → 节点结论的完整推理树；
- 机会候选、风险点、假设、失效条件和限制。

`review-and-finalize` 以 Agno `workflow_run_id` 作为幂等身份，原子写入：

```text
data/investment/conclusions/<workflow_run_id>.json
```

该 JSON 是 Workflow 的产品输出，同时作为最终 Step `content`返回。它包含命题、决策时间、一句话结论、分层结论、节点趋势、推理树、Reviewer 结果、限制和上下文指纹；不包含 Agno `step_results`、重复的全量上下文或隐藏思维链。同一 `workflow_run_id` 重试只能接受完全相同的 Artifact，内容冲突必须失败，不得覆盖。Data Service 发布留待后续独立工序。

## 分层结论与根谱

分层中间结果只保存在本次 Workflow 状态中。核心合同包括：

- `InvestmentReasoningInput`：Schedule 命题和时间窗；
- `AnalysisAnchorSnapshot`：标准锚点身份；
- `ImpactClaimProposal` / `AcceptedImpactClaim`：待校验和已接受的分层影响结论；
- `LayerImpactBatch` / `LayerAnalysisContext` / `LayerAnalysisResult`：单层上下文与输出；
- `GeopoliticalAnalysisState`、`MacroAnalysisState`、`IndustryAnalysisState`：逐层继承的 Workflow 状态。

模型只能在 `ImpactClaimProposal` 中提交标准锚点业务 ID、Variable ID、方向、周期、置信度、摘要、机制，以及可验证的 `source_fact_ids`、`mechanism_fact_ids` 和 `parent_claim_ids`。模型不得自行声明 Event/Signal 根谱，也不得生成 `claim_id` 或层级。

确定性门禁接受 Proposal 后生成 `AcceptedImpactClaim`，并补充：

- 稳定 `claim_id`、所属层级、标准锚点名称和类型；
- 从 `source_fact_ids` 或父结论解析出的 `root_event_ids` 与仍有效的 `root_signal_fact_ids`；
- `DIRECT_SIGNAL` 或 `CROSS_LAYER` derivation；
- 已验证的机制 Fact、假设、风险和失效条件。

门禁规则：

1. 直接结论必须引用作用于同一标准锚点的有效 Signal Fact。
2. 跨层结论必须引用已接受的前序层父结论和真实机制 Fact；同一条机制 Fact 必须同时连接父结论锚点和当前锚点，再由代码继承父结论的全部根谱。
3. 后序层不能引用同层或未来层结论，不能形成循环谱系。
4. 派生结论的影响周期不能超出根 Signal/父结论支持范围。
5. 派生结论置信度不得高于其最弱父结论和机制证据。
6. 普通 Fact 可以解释机制，但不能作为方向根；MENTIONS 只表示相关。
7. `NO_MATERIAL_CHANGE` 也需要充分证据；单纯缺少证据时必须使用 `INSUFFICIENT_EVIDENCE`。

## Graphiti 检索原则

- 语义召回使用 Graphiti 原生 `search` / `search_`，不引入额外向量中间件或 Graphiti 源码分叉。
- Event 时间窗、`group_id`、标准锚点身份、Fact 有效期、节点和拓扑使用 Graphiti 已配置 Neo4j driver 精确读取。
- `prepare` 只获得共享基础数据；geo、macro、industry 各自按前序已接受结论主动补充本层数据。
- Agent 不获得无限制 Neo4j Tool。补充检索请求必须满足允许的锚点类型、数量、文本长度、并发和结果上限。
- Graphiti 原生语义命中只产生候选；方向结论仍必须通过根谱门禁。

## 运行与迁移

Workflow 合同变化必须提升 `investment_reasoning_contract_version`，由启动时的 code-governed migration 发布新 Studio 版本。Reasoner 和 Reviewer 提示词合同变化也必须提升各自 contract version。

Schedule 行由 PostgreSQL/Control Panel 持有，修改 seed 默认值不会覆盖已有 Schedule。上线前必须人工确认现有 `/workflows/investment-reasoning/runs` 的 Payload 符合 `InvestmentReasoningInput`，并确认同一 endpoint 只有一条启用 Schedule。
