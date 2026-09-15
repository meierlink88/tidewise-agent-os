---
name: run-investment-report
description: 由 Codex 编排新增入图 Event、Research 地缘冲突研究、地缘报告提炼、宏观经济与产业链独立分析、统一 v6 数据包及 Data Service 发布。用于完整投研批次或断点恢复，不注册 AgentOS 工作流或定时器。
---

# 完整投研报告工作流

Codex 是执行者；本 Skill 是六步工序合同，脚本只控制确定性数据、身份、状态与发布动作，不启动模型。配置或改造本工作流不等于授权立刻运行真实业务批次。实际运行前明确数据环境、Research/Data 地址、时间窗口和是否发布；用户已明确的信息不重复确认。每次调用本 Skill 都先区分“编辑工作流”与“运行报告”。

依赖：[宏观经济与产业链分析](../analyze-macro-industry/SKILL.md)，以及本机 `/Users/meierlink/.codex/skills/compose-geopolitical-report/SKILL.md`。实际安装路径不同时发现并读取真实路径。工程/服务变更另遵守 ganchaojia-development-standard；本流程不自行修改服务合同。

## 共同约束

- 输入是本批新增入图 Event，字段固定 `Event.created_at`，窗口 `[start,end)`，不包含旧 Event 更新。先冻结 end，再取数。首次窗口由用户指定；只有明确授权按水位连续运行时，才用上次成功批次 end 作为 start。查询失败不是零事件。
- 三层分析用同一批次窗口；原文引用历史市场数据仍保留各自统计期。Research 自行通过工具读取该窗口故事线的 Event/Fact Signal；这是相同筛选口径，不代表 Research 和冻结 CLI 输入构成数据库事务级同一快照。保留 Research 实际工具查询记录与最终报告绑定。
- 确定身份用真实 story_id，名称不当ID；每故事线每批次一次 Research。一个 Event 可关联多个故事线；Codex 审核相关性并记录采用/跳过原因，不用关键词脚本代替判断。
- 宏观只用 MACRO_ECONOMIC 原始输入；产业独立用 GEOPOLITICAL、MACRO_ECONOMIC、INDUSTRY_CHAIN、COMPANY 四类输入。双方不读取 Research 最终报告、地缘提炼内容或兄弟分析结论。阶段有先后，研究结论没有串联依赖。
- 不删改宏观/产业变量综合、冲突处理、公司→节点→链传导、Concept真实N:M聚合与无Concept归属ICH独立总结。公司数据保留内部审计，不产生公司产品章节。
- `content.json`、内部 `v6-draft` 和正式 `report-publication/v6` 是三种不同用途的产物。只有正式 v6 单元可进入三层包。阶段内先完成投影；最终合并不兼容旧格式、不重写结论、不统一跨层方向。

## 六步执行

1. **取数并确定地缘故事线。** 先读宏观/产业 Skill 的取数合同与 AgentOS `capabilities/report_reasoning/README.md`，核验环境和源身份，经已有内网/SSH运行 `prepare --live --time-field created_at`，逐页核对 Event、Signal、Evidence 和真实实体。传回冻结输入并核验哈希。调用 `workflow.py init` 固定半开窗口；其输出 `input/` 可供原查询CLI使用。Codex阅读全文审核全部新Event，列出有关联地缘故事线及每条是否值得本批研究，保存 selection 并调用 `select`。Graph MENTIONS是候选关系，不替代业务判断；没有Signal也必须审阅Event。
2. **逐故事线调用 Research。** 对每个 decision=research 的故事线执行 `research-submit` 一次，再分次 `research-poll`。请求只含名称、story_id、market、精确window；不将Event/Signal正文拼入crisis，不伪造agentos_workflow_run_id。保存确切run_id、最终报告及SHA256。完成屏障要求全部选中故事线成功；失败不自动跳过，POST结果未知不重新POST。没有需研究故事线时记录原因，合法跳过研究。
3. **地缘提炼与 v6 包。** 全部研究完成后，按 compose-geopolitical-report 逐份从原始首席报告提炼，一故事线一单元。按该Skill进行资产筛选、图谱简称匹配、单论点、箭头摘要、单值指标与幅度取舍；查询故事线Event关联Evidence，审计留外部。除可读内容外，按其 v6-mapping 编排完整正式v6单元。缺少合同要求不补造。Codex完成内容审阅后用 `pack --lane geopolitics` 冻结地缘包；没有候选时包内地缘数组为空并留来源说明。
4. **宏观及产业独立推理。** 地缘包完成后启动两个独立Codex分析上下文，可使用两个子代理。每个只收到本路范围、同批冻结原始输入、方法文件和独立输出目录；不fork主上下文历史，不发兄弟结论。调用宏观经济与产业链分析Skill，两路方法完全沿用。保存各自原件、audit/review，原CLI validate与人工语义覆盖审查都要完成。无独立上下文能力时如实阻断，不在读过Research结论的上下文冒充独立分析。
5. **宏观与产业 v6 包。** 原件的变量综合不丢失，只不发布。分别执行 `project_v6.py`，使用事先冻结SHA的Data官方转换器将旧内部结构投影为正式v6；图谱简称按宏观/产业Skill的发布名称规则处理并复核。每条概念仍保留各链独立reasoning，无归属链仍在industry_chain_analyses。审阅投影及来源后分别 `pack`；原件缺陷回原分析工序修正，不靠转换补推理。
6. **统一装配、校验、发布与读回。** `assemble` 只合并三层对应数组，检查共同元数据及故事线覆盖，产生 `{publisher_report_id, report}`；不把三层分别发布为互相覆盖的快照。`validate` 调用Data官方Go校验器；再核验部署合同、目标及Evidence存在性。已授权发布时 `publish`，按确切report_id执行分批可恢复的 `readback.py`，Codex逐字段及Evidence内容对照，完全相同包重放一次，提交绑定该包的读回review后 `complete`。只有这时才称整批完成。

## 数据包与状态

三个包使用同一个正式 v6 Report 外壳：地缘包只填geopolitical_stories；宏观包只填macroeconomic_stories；产业包填concept_analyses与industry_chain_analyses。其余集合为空。元数据必须相同；局部键不在merge中修复。analysis_window为`{start,end}`，不是start_at/end_at。包合并的机械步骤没有语义转换。

脚本只证明哈希/结构/顺序等约束；Codex签写review是语义审阅记录，不是机器自动证明推理正确。所有外部接口都需要实时核对其合同及部署身份。

先读 [脚本接口与运行文件](references/commands.md)，需要理解当前差异时读 [合同与发布边界](references/contracts.md)。不把未执行阶段写为通过。每次脚本只执行一个明确工序，主Codex读取返回状态继续；没有后台调度，也不创建AgentOS工作流。
