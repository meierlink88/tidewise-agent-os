# Codex 报告分析师工程

Codex Agent 是分析师，负责完整的数据查询决策、三次独立推理、修正和最终报告生成。AgentOS API 或本目录 CLI 是数据查询工具。本目录不启动 Codex、不调用模型、不编排分析、不自动生成或组装报告。产物为 `report-publication/v5` JSON；不发布 Data、不生成 HTML。

执行入口：让当前 Codex Agent 阅读 [ANALYST.md](ANALYST.md)，随后由它使用工具完成工作。没有 `run-branch`、`assemble`、模型调用、自动修复循环或长时间全局锁。查询和校验失败会返回非零退出码，由分析师决定下一步。三路可以在独立 Codex 上下文中执行；跨上下文的安排属于 Codex 任务操作，不属于本 CLI。程序不会擅自创建新的 Agent。

## 数据工具

在 AgentOS checkout 中激活 Python 3.12 虚拟环境。实时模式在能访问图谱和 Event journal 的 AgentOS 主机运行；NEO4J_URI/USER/PASSWORD 由该主机运行环境提供。跨机器可以经用户已有 SSH 通道在 AgentOS 主机执行 CLI，不需要把数据库凭据交给 Codex 客户端，不新增公网入口。

```bash
# 实时原始数据冻结：由分析师选择明确时间窗
python -m capabilities.report_reasoning prepare --live --event-root /path/to/data/event --start 2026-09-01T00:00:00Z --end 2026-09-07T00:00:00Z --run /path/to/input-001
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
