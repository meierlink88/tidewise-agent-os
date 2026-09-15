# 宏观经济与产业链：正式 v6 发布投影

分析原件仍用原 variable-report.schema.json 的 v6-draft，变量综合、冲突和传导方法不变。发布出口统一为正式 report-publication/v6，不再执行旧v5发布流程；不能仅更改schema_version。

1. 每路原件独立审阅与CLI validate；geopolitical_stories与company_analyses为空，保留Concept聚合和无归属链。公司变量与判断在audit。
2. 执行 ../run-investment-report/scripts/project_v6.py（从本Skill根定位同级Skill目录），显式提供原件、真实名称目录、Data仓库官方report-unify.py及其冻结SHA256。脚本保存原件，移除发布不展示的variable_assessments，转换industry_chains/affected_nodes为reasonings/affected_assets，转换首页引用。不会填造指标、条件、预测期或置信度。
3. reasoning_summary、assessment、Signal原文及来源、反证、真实图和节点判断保持原分析含义。脚本从正式Data转换器执行结构投影，不能以转换成功代替语义和目标服务校验。无需量化内容时reasoning_blocks允许为空，不把地缘单值指标卡规则强加于宏观与产业推理。
4. 按publication-names.md投影真实简称，保持ID和引用。审阅所有投影差异后，向总编排交出正式v6单元包与review；原件仍保留全部变量综合。
5. 发布、目标Evidence预检、逐页读回和幂等重放由run-investment-report统一执行。不得单独发布宏观或产业包抢占完整快照；独立调用本Skill要求发布时也使用该发布尾段，明确空地缘范围，不重新启动地缘研究。

正式结构以Data当前OpenAPI与Go验证器为真源。旧publication-v5.md仅保留历史案例，不可作为新运行出口。置信度等既有宏观/产业分析要求继续保留；地缘产品缺省规则不扩散到本方法。
