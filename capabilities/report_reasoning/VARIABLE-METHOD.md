# 变量综合判断：v6-draft

Codex 是分析师，AgentOS CLI 只查询数据。沿用 ANALYST.md 的三次独立范围，不读其他工序或旧报告结论。采用基本面定性分析，不做量化打分、信号多数投票或机械加权。本方法覆盖旧方法中逐条信号直接转为结论的处理。

先审阅全量 Event 与原始 Signal，按同一真实锚点、同一变量整理。可比口径内同向信号合成一个变量判断，源文作为证据；同源转载不能计为多份独立支持。方向冲突时分析时间先后、发生或计划、地域产品口径、来源可靠性及经济机制：判断抵消、仍升、仍降、真实分化或无法确认。没有可比量级不能声称已精确抵消；不可比范围分开 scope/timeframe 并解释。

变量综合判断 → 锚点基本面影响 → 投研方向 warming/cooling/diverging（不足时 pending）→ 关联锚点传导。变量 UP 不等于投研 warming；说明需求、供给、成本、利润和现金流机制。地缘→宏观→链→节点，公司→节点→链；产业内部独立使用四类原始数据，不读另两路的结论。

使用 internal/variable-report.schema.json，版本 report-publication/v6-draft。正式 v5 合同保持原样；本稿不宣称现有 Data Service 可接收，不发布。

在 variable_signals 相同所属位置新增 variable_assessments：故事线在 detail 中，宏观/链/节点/公司在各实体自己字段中。字段为 local_key、variable_id/name、scope、timeframe、direction（UP/DOWN/STABLE/MIXED/UNKNOWN）、synthesis、conflict_resolution、support_signal_ids、counter_signal_ids、excluded_signal_ids、evidence_ids。

每个原始信号归入相应变量综合判断的一个证据类别，三类互斥；每组至少引用一条自身信号。同一可比变量原则上一组，不可比范围拆分须解释。反向信号不能为了得到一致方向而排除；excluded 仅表示错配或不适用。原始行逐字段保留，综合判断不能改写原始方向。无自身信号实体的两数组均为空，通过上游综合判断形成 inferred 结论。

HTML 默认展示每变量一条综合判断与综合方向，原始多条信号折叠为支持、反向/冲突、不适用证据。不再把逐条原始信号当成独立变量判断展示。

所有有信号锚点必须判断，包括错挂或失效后的否定/待确认判断。正文可用 qualified 行保留原始来源并明确它不能支持目标变量变化。产业内部上层锚点在 audit 保留同结构变量综合判断和传导依据，不强造产品章节。无可推导结果的节点不展示。

audit 保留逐 Event/Signal 处理记录、变量综合判断及结论位置；review 说明合并、冲突判断、范围拆分及传导。不能因为报告长而只选示例。结构校验不代替 Codex 语义自查。
