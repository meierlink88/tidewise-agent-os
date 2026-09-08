# Codex 报告分析师工程

变量综合试验见 [VARIABLE-METHOD.md](VARIABLE-METHOD.md)。新增 `variable_assessments` 使用 `report-publication/v6-draft`，保留原始信号作为证据，由 Codex 定性解决冲突后再传导。CLI 同时校验 v5 和新草案；Data Service 当前 v5 不承诺接受新字段，不执行发布。

HTML 是固定推理工序外的可选展示：`python -m capabilities.report_reasoning.tools.render --report /path/to/report.json --evidence-catalog /path/to/evidence-catalog.json --output /path/to/new-preview`。复用归档 v8 样式，默认显示变量综合判断，原始支持/冲突/不适用信号折叠展示；不会修改输入报告或生成分析结论。

Codex Agent 是分析师，负责完整的数据查询决策、三次独立推理、修正和最终报告生成。AgentOS API 或本目录 CLI 是数据查询工具。本目录不启动 Codex、不调用模型、不编排分析、不自动生成或组装报告。产物为 `report-publication/v5` JSON；不发布 Data、不生成 HTML。

执行入口：让当前 Codex Agent 阅读 [ANALYST.md](ANALYST.md)，随后由它使用工具完成工作。没有 `run-branch`、`assemble`、模型调用、自动修复循环或长时间全局锁。查询和校验失败会返回非零退出码，由分析师决定下一步。三路可以在独立 Codex 上下文中执行；跨上下文的安排属于 Codex 任务操作，不属于本 CLI。程序不会擅自创建新的 Agent。

## 数据工具

在 AgentOS checkout 中激活 Python 3.12 虚拟环境。实时模式在能访问图谱和 Event journal 的 AgentOS 主机运行；NEO4J_URI/USER/PASSWORD 由该主机运行环境提供。跨机器可以经用户已有 SSH 通道在 AgentOS 主机执行 CLI，不需要把数据库凭据交给 Codex 客户端，不新增公网入口。

```bash
# 实时原始数据冻结：由分析师选择明确时间窗
python -m capabilities.report_reasoning prepare --live --event-root /path/to/data/event --start 2026-09-01T00:00:00Z --end 2026-09-07T00:00:00Z --run /path/to/input-001
# 按北京时间 9 月 8 日新增入图 Event 取数，包含这些 Event 的全部关联信号
python -m capabilities.report_reasoning prepare --live --event-root /path/to/data/event --time-field created_at --start 2026-09-07T16:00:00Z --end 2026-09-08T10:51:23Z --run /path/to/input-created-001
# 或历史原始数据回放：不可称为实时报告
python -m capabilities.report_reasoning prepare --source /path/to/raw-export.json --run /path/to/input-001
# 分页阅读本工序全部 Event、Signals、Evidence
python -m capabilities.report_reasoning query --run /path/to/input-001 --branch geopolitics --resource events --limit 100
python -m capabilities.report_reasoning query --run /path/to/input-001 --branch geopolitics --resource signals
# 按真实实体查询其信号、原始关系；结构查询返回一跳关系，由分析师继续查询
python -m capabilities.report_reasoning query --run /path/to/input-001 --branch geopolitics --resource entities --text 航运
python -m capabilities.report_reasoning query --run /path/to/input-001 --branch geopolitics --resource structure --id ICH...
python -m capabilities.report_reasoning query --run /path/to/input-001 --branch geopolitics --resource signals --id GPR...
# Codex 自行写好最终 JSON 后调用，程序不修改报告
python -m capabilities.report_reasoning validate --run /path/to/input-001 --report /path/to/report.json --receipt /path/to/check.json
```

`query` 返回 total/next_offset/items 和冻结输入哈希。每次携带相同筛选条件与 next_offset，直到 null。Event、Signal、Evidence 按工序隔离；实体身份及结构可全目录查询，它们不等同于影响证据。输入不含其他工序结果。CLI 不提供读取兄弟结论的命令，但不能隔离 Codex 已存在的会话记忆，严格独立须使用独立上下文。

实时适配读取 Neo4j 和本机 storyline journal，检查读取期间漂移及选中 Event 的发布完成状态；不宣称全采集队列完成或跨系统事务快照。当前支持的 journal 格式须与目标环境验证，不支持时应失败，不得冒充空业务结果。API 适配尚未实现；第一版使用 CLI。

## 验证边界

JSON Schema 来自定稿 v8 / Data v5 合同。辅助校验检查字段、顶层类型、工序来源、信号归属与原文、局部上游引用和真实链成员/边；返回具体字段路径。语义合理性、全量分析覆盖及报告总结/详情一致性由 Codex 自查，命令通过不能代替这些判断，也不等于 Data Service 已接受发布。

旧 Python 编排试跑已取消，原始输入与失败记录保留在运行目录，不将其输出视为新工序结果。现有 `capabilities/investment/` 完全不变。Issue #213 / 人工 PR 合并。第一版不包含定时任务、DGX 部署、Agno 升级。

```bash
python -m unittest capabilities.report_reasoning.internal.test_tools -v
./scripts/validate.sh
```

## 本次整改验收

使用 v8 同期原始快照逐页查询（每页 17 条），核对总数与唯一 ID：地缘 38 Event / 22 Signal / 39 Evidence；宏观 21 / 6 / 22；产业 168 / 77 / 172。此结果仅证明历史输入查询完整，不代表完成新的业务推理。原始 Event 的 valid_at 可含未来生效时间，其最小/最大值不能自动作为本次报告分析时间窗；分析师须明确报告统计口径。实时目标环境未验收。

时间字段：`--time-field valid_at|created_at` 仅用于 `--live`，默认 `valid_at` 保持兼容。两端均包含，截止时间不能在未来。`created_at` 指 Neo4j Event 入图时间，不是新闻发布时间或 Data Service 入库时间；未来生效计划可按其已入图时间纳入。关联信号按 Event 引用取齐，不按 Signal 创建时间过滤。快照 Event 同时保留两种时间，manifest/query 返回 `selection_time_field`；历史导出无口径时标记 `unspecified`，不猜测。跨窗口 Signal 来源闭包仍须完整，不支持自动扩展输入。

## 产业链分支的概念聚合（当前强制规则）

产业链工序仍独立使用四类 Event 及关联信号，先评估节点和产业链，再按真实 Concept 聚合为 `concept_analyses`：一概念一套 summary/detail，detail.industry_chains 中每条链有独立因果链详情。概念自身没有 Signal 时为 inferred，不能把链/节点/公司信号挂成概念自身信号。概念总结需综合需求、供给、成本、利润机制以及支持/反证，给出综合方向与影响度，不能只拼接链结论。

取数必须包含 Concept 正式 ID/名称和 IndustryChainMappedToConcept 的真实有效关系；缺失能力应补齐，不得以独立链总结替代概念聚合。只纳入有当期可成立影响判断的链和节点。图谱关联是分组候选，不是影响证据；尤其公司生态、技术生态概念，要证明此次链敞口适用于该概念，不因同属一链自动传播。

多对多关系保留，不强制一链一概念；逐关系审查适用性，适用时可在多个概念中出现同一链，来源和统计去重。同概念下同一链仅一份综合详情，local_key 在单元内唯一且引用闭包。产业总结的受影响锚点沿用已评估产业链节点口径，引用详情节点并带 chain_local_key；概念聚合只改变总结根实体和多链组织，不引用概念自身。没有有效 Concept 映射的已评估产业链，按用户明确要求以产业链正式名称作为独立总结单元，用 industry_chain_analyses 和真实 ICH ID 发布，一链一套总结及因果链详情；产品上与概念总结并列，不伪造 CON 身份或图谱关系。有映射但证据不适用于该概念时记录限定/排除理由，不能把映射存在视为已经受影响。公司产品层级继续为空。

既有报告的结构修复允许保留原冻结 Event/Signal 及不受影响的地缘/宏观工序，但须记录复用来源和哈希；另取的概念/映射须单独记录实际查询时刻与来源，不能伪称旧时点快照。产业工序独立从原始输入分析概念适用性和综合结论，不读取地缘/宏观工序结论。
