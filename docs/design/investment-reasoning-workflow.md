# Investment Reasoning Workflow

## 目标

`investment-reasoning` 是一条由 Schedule 的自然语言命题驱动的固定投研推理链。默认命题使用最近 48 小时 Event，识别受影响产业链，并对真实产业链节点给出升温、降温、分化、无显著变化或证据不足。

Schedule 只是触发器和命题来源，不参与推理。Agno PostgreSQL 保存 Workflow 运行状态和最终结果。

## 边界

- `capabilities/investment/`：投研合同、五个 Workflow Function、Signal 根谱验证、结论归一化和 Agent 调用编排。
- `agents/investment_*.py`：Planner、Reasoner、Reviewer 三个 Studio 组件。
- `workflows/investment_reasoning.py`：仅组装五步固定 Workflow，不实现业务规则。
- `sematica/graphiti/investment.py`：仅使用 Graphiti SDK 的 `search` 和 Neo4j driver 返回带 group/time/容量边界的原始图记录，不包含投研策略。

旧的 `sematica/reasoning/investment` DAG、CLI 和 Graphiti LLM 直接调用已删除，避免出现第二条推理通道。

## 五个阶段

1. `plan-investment-analysis`：Planner 把 Schedule message 转换为命题和事件回看时间窗；产业链上限、前瞻范围和传导跳数由 Workflow 确定性代码持有。
2. `prepare-investment-context`：冻结 `decision_at`，检索时间窗内 Event、相关 Fact、Signal Fact、产业链和拓扑。
3. `reason-signal-transmissions`：Reasoner 最多执行三轮。确定性代码在每轮后校验 Signal 根、节点和真实拓扑边。
4. `synthesize-investment-conclusion`：Reasoner 综合每个真实节点的短、中、长期结论，确定性代码把无支撑的周期归一化为 `INSUFFICIENT_EVIDENCE`。
5. `review-and-finalize`：硬门禁先检查根谱，Reviewer 再审核证据、拓扑、周期和结论语义。审核拒绝后只允许一次有界修正；仍不通过则降级为无方向性断言的安全弃权。

## 关键投研门禁

Event、MENTIONS 和普通 Fact 可以用来召回相关产业链，但不能启动方向性推理。第 1 跳传导必须引用一条当前有效、方向明确、影响周期明确且作用于源节点的 Signal Fact。第 2–3 跳必须引用已接受的上一跳传导，并继承原始 Signal Fact ID。

因此：

- Event 关联到产业链仅表示相关，不自动生成升温或降温。
- 没有有效 Signal Fact 时，Workflow 不调用传导模型，节点方向统一为证据不足。
- 普通 Fact 可以作为机制解释的背景，但不计入周期方向支撑。

## Graphiti 检索

语义召回使用 Graphiti 原生 `Graphiti.search`。时间窗内每条 Event 都用“标题前 16 字 + 摘要前 8 字”进入召回输入，每 20 条一批，与命题查询一起最多 4 个并发，然后按 Fact UUID 去重。这个短摘要上限同时避免 Lucene `TooManyClauses`；在 500 条 Event 的容量上限下，原生搜索调用严格不超过 26 次。普通 Fact 只有命中原生召回才进入 Agent 上下文；Signal Fact 为了安全根谱检查保留完整集合。时间窗、`group_id`、Event 类型、标准节点和拓扑精确范围使用 Graphiti 已配置的 Neo4j driver 读取；不引入 Qdrant、自定义向量中间件或 Graphiti 源码分叉。

## Schedule

新环境显式 seed 默认创建 `/workflows/investment-reasoning/runs`，上海时区每日 07:30 执行。该 cron 和 message 仅是首次创建默认值；之后由 PostgreSQL/Control Panel 管理，应用启动不覆盖。
