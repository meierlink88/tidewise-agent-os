# 地缘冲突研究 Workflow

## 目标与归属

AgentOS 负责发现需要研究的正式地缘政治故事线、可靠调用研究团队及保管报告引用。
Tidewise Research 的 `geopolitical_war_room` 负责原有四角色研究和首席策略师汇总。
本 Workflow 不再执行原七阶段投研推理或 Data Service 发布；外部 Codex 后续按明确的
Research run ID 获取全文，完成提取和发布。旧 Investment 能力和 Studio 历史版本保留。

Workflow ID 仍为 `investment-reasoning`，显示名称改为 **地缘冲突研究**，合同版本由 13 升到 14。
这保留 `/workflows/investment-reasoning/runs`、既有 Schedule endpoint 和历史审计身份。
代码迁移发布新 Studio 版本一次；相同版本重启只校验并加载，不反复发布。

## 两阶段编排

1. `筛选24小时新增事件故事线` Function：固定 `cutoff_at`，读取 `[cutoff_at - 24h, cutoff_at)`
   内 `Event.created_at` 新增的 Event，通过同 group 的正式 `MENTIONS` 关联找到
   `Entity:GeopoliticRivalry`，保存 `plan.json`。
2. `逐条故事线研究` 原生 Agno Loop：每轮 `调用地缘冲突研究团队` Function 消费一条故事线，
   等待完成或到达等待上限，保存该条报告/失败状态后进入下一条。

判定标准是“本窗口有至少一个正式关联的新增 Event”，不再让模型重新判断关联或加主观重要性阈值。
同一故事线多个 Event 合并，同一 Event 可以触发多个正式关联的故事线。无命中时返回 `no_change`，
不访问 Research。筛选不以新闻发布时间、Event 发生时间或 Signal 产生时间替代 `created_at`。
新增入库不等于新的现实事实；本流程复用 Event 上游身份消歧结果，不另行进行语义去重。

单次图查询最多允许 10,000 条不同故事线/Event 关联，超过时拒绝整个选择而非悄悄截断。
原生 Loop 最多 1,000 条故事线，选择阶段提前校验。坏身份、冲突内容及存储失败中止执行。
图查询为单次只读事务；plan 固定该次已读取内容，不能把它解释为图的历史时间旅行快照。

## 输入与滚动窗口

新输入支持 `{"market":"A股市场","cutoff_at":"2026-09-15T07:30:00+08:00"}`；
省略 cutoff 使用运行开始时的 UTC 当前时间。必须提供带时区时间，禁止未来截止时间。
`market` 默认为 A股市场。原 Schedule 自然语言或 `question/event_window_hours/include_company`
旧 envelope 仍接受，但这些旧控制项不改变固定 24 小时行为。未知字段拒绝。

AgentOS固定滚动窗口并保存选择plan，但对外只传故事线名称、真实ID和窗口元数据。
Research按这些参数使用工具自行查询Event与关联Signal，不使用嵌入crisis的数据包。
查询沿用Signal来源闭包检查及完整Event分页，不伪造自然日，也不宣称是不可变数据快照。

## Research API 交接

POST /swarm/runs 使用 preset_name=geopolitical_war_room，user_vars 中 crisis 仅为故事线名称。
传入真实 story_id、market、event_window_start/end、agentos_workflow_run_id；research_date 留空。
不发送 Event 或 Signal 正文。Research 角色使用 AgentOS MCP 工具按同一半开窗口查询
Event.created_at 和关联 variable_signals，并按需读取 Evidence，保持原角色、方法和报告结构。
两工具同时支持旧 research_date 自然日模式，但拒绝混用、缺半边、无时区、倒序或未来窗口。
配置 Research 的 VIBE_TRADING_SWARM_AGENT_CONFIG，以 agentos 为 MCP server key。
工具必须在运行前注册可用，不能把“生成了报告”当成“成功查询了 Event/Signal”。

GET /swarm/runs/{run_id} 按准确 run ID 与原 user_vars 校验；完成后归档完整 final_report，
记录 SHA256 与字节数。查询为 live_not_snapshot，冻结窗口不保证数据内容不再变化。

默认每 5 秒 GET 一次，每条最多等待 7,200 秒；HTTP 单次超时 30 秒。
分别由 `TIDEWISE_RESEARCH_POLL_SECONDS`、`TIDEWISE_RESEARCH_WAIT_SECONDS` 配置。
单条轮询超时/HTTP 异常保留已知 run ID 并继续下一条，下一次命中相同输入时仅恢复 GET。
终态失败保留，不自动再研究。基础设施配置或持久化错误中止 Workflow。

## 持久化、并发与失败边界

`GEOPOLITICAL_RESEARCH_ARTIFACT_ROOT` 默认为 `data/geopolitical_research`，需持久化挂载：

```text
runs/<workflow_run_id>/plan.json    # 不变的24小时选择和完整输入
runs/<workflow_run_id>/result.json  # 累积逐条结果及报告引用
jobs/<input_hash>/receipt.json      # origin、完整请求、远端run ID、状态、报告hash
jobs/<input_hash>/report.md         # API final_report 的完整UTF-8内容
```

job identity 由交接合同v2、preset、market、窗口边界、故事线和排序后的完整 Event 集合生成。
相同窗口和输入可复用；窗口或Event内容变化产生新job，避免复用v1未查询Signal的报告。
返回原收据的 source_workflow_run_id，不能把旧报告伪装成本次新研究。
本地非阻塞文件锁覆盖单 run 和单 job。并发命中同一 job 时记 `research_busy`，不二次提交。
此保证要求所有实例共享支持 flock 的同一持久化目录；不声称支持独立磁盘多副本。

先写 `submitting` 再 POST，拿到 ID 后立即持久化为 running。
如果 POST 结果不确定或进程在收到 ID 前崩溃，标记 unknown/submitting，禁止自动重发。
现有 Research API 无创建幂等键，因此不能承诺跨 HTTP 故障的 exactly-once。
运维需核对 Research 中 user_vars 的 workflow/story/window 元数据后修复已确认的远端 run ID；
不删除不确定收据来强行重试。已知 ID 的超时可由新的工作流运行重新选中相同事件后继续查询；
若事件已离开窗口，需基于原 plan/receipt 显式恢复，当前版本没有独立恢复管理 API。

工作流 `outcome` 为 completed / no_change / partial / failed；每条保留 status/error_code。
Agno 的 completed 只表示编排执行完毕，消费者必须同时检查业务 outcome 和每条状态。
最后结果包含 research_run_id、origin、report_path/hash/bytes；完整请求只在 plan/receipt 保存，
不把大篇事件内容在各 Loop 输出中重复展开。

## 调度和上线

默认 seed 保持每日上海时间 07:30 及原 endpoint/name；内容改为地缘冲突研究。
现有 PostgreSQL Schedule 仍由运维/Control Panel 持有，启动不创建、不覆盖、不恢复启停状态。
旧 payload 可兼容但会按新固定24小时规则解释；运维可以主动更新显示文案/payload。
部署前确认一个 endpoint 只有一个启用 Schedule，配置 Research URL/鉴权/工具与持久化卷。
长批次的调度租约、HTTP 入口超时应足以覆盖串行研究总时长；单 job 锁只能避免同输入重复提交，
不能替代调度容量规划。

本 PR 不部署环境、不启动真实付费研究、不操作 Data Service 发布。
回滚使用旧代码及原 published Workflow 版本；不要删除研究收据或历史报告。
若运行新代码启动，版本合同会再次迁移到14，故回滚需要同时回滚代码。

## 验证

`python -m unittest discover -s tests -p 'test_geopolitical_research*.py' -v`
覆盖窗口边界、重复关联、身份冲突、零命中、串行失败继续、报告全文/CRLF保真、
不确定创建不重发、已知ID轮询恢复、输入幂等、Studio历史版本迁移以及真实本地 REST/MCP 传输。
外部 Research 和图输入在测试中隔离；不把隔离合同测试当成已部署服务联通证明。

### DGX 内网接入

Research 与 AgentOS 加入 `tidewise-agentos-uat-private` Docker 网络，服务别名为
`tidewise-research`，无需发布任何宿主机或公网端口。AgentOS 内部调用地址为
`http://tidewise-research:8899`。Research 保留出站网络以访问模型和资料源。

在 GitHub `uat` environment 配置 variable `TIDEWISE_RESEARCH_BASE_URL` 及 secret
`TIDEWISE_RESEARCH_API_KEY`（与 Research 的 `API_AUTH_KEY` 相同）。部署脚本将这些值
写入受保护的 runtime.env，再由 Compose 注入容器；不要只手动修改现有容器或 runtime.env，
否则下次部署会丢失配置。等待/轮询参数也可用同名 environment variables 配置。
