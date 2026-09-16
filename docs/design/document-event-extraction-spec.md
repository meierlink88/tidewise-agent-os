# 文档级 Event 提取合同

状态：用户已确认的目标合同；本文件不表示当前运行时已切换。追踪：AgentOS #262、Data #513。

## 定义与范围

一个已登记的 Raw Evidence 文档版本对应一个 Event。阅读全文后生成整篇 title、summary、keywords；不按日期、主体、动作或原子事实把文章拆成多个 Event。不同 URL/正文版本的合并不属于本合同，重试不得重复生成 Event。

Evidence 中间提炼层退出；最终移除 Data 的 evidences 表。Event 通过 event_evidence_links 直接关联 raw_evidences，而不是通过 Atomic Evidence。Raw 元数据和 MinIO 正文保留。

本轮 Data 结构交付不自动部署、不清除历史数据、不启动重提取。AgentOS Workflow、图谱、报告消费者须协调升级后才能切换。

## Event 字段

| 字段 | 合同 |
| --- | --- |
| id | Data 分配正式 EVT ID，模型不生成 |
| title | 整篇事情的一句话事实总结，无任意字符上限 |
| summary | 忠实提炼整篇原文；保留主体、动作、对象、数值、单位、比较口径、统计期间、条件、否定、计划及不确定性；不添加假设或推理 |
| keywords | 0–5 条关键量化事实字符串；不是主题词 |
| collected_at | 程序记录 Event 提取完成的 UTC 时间，冻结后重试不变；不是文章采集时间或数据库写入时间 |
| published_at | 原新闻发布时间，复制 Raw 值；未知为 null，不以采集时间替代 |
| status | Data 生命周期状态，不表示事实确认程度 |
| semantic | API 数组；PG 使用 event_semantics 子表，无 events.semantic JSON 列 |

keywords 每条保留相关主体/对象、指标、数值、单位及必要期间/限定词。超过五项按核心相关性与信息量选择，其余重要指标保留在 summary。不可自行计算指标、把日期/编号当指标、删除“至少/约/预计”等限定。没有量化事实返回 []。不以阿拉伯数字正则判断量化事实。

## 语义事项子表

每行包含 id、event_id 以及下面十个业务字段；没有 position，不承诺原文顺序。主体和对象是原文描述，不是实体外键。读取可按 id 保持稳定顺序，但顺序不表达业务意义。

| 字段 | 类型/含义 |
| --- | --- |
| actor | string/null，执行主体 |
| action | string，执行动作 |
| target | string/null，作用对象，可为组织、国家、产品、政策或合同等 |
| announced_time | string/null，宣布时间 |
| effective_time | string/null，原文规定的生效时间，不自动代表已生效 |
| planned_execution_time | string/null，原文明示的计划/预定/预计执行时间 |
| executed_time | string/null，原文明示已经执行的时间 |
| statement_type | POLICY / GENERAL，政策 / 普通 |
| action_status | PLANNED / OCCURRED，所描述动作计划中 / 原文声称已发生 |
| assertion_status | CONFIRMED / UNCONFIRMED，原文明确确认 / 未确认，不代表系统独立核实 |

四类时间保留文本，支持具体日期、季度、年份、某周及相对表达；不强制标准日期或猜年份。原计划与实际执行时间可同时保留。不得依据当前日期把计划自动变成已执行。

对象提取成立条件：动作明确，actor/target 至少一个可识别，四类时间至少一个有原文依据。不满足时不生成该子对象，但事实仍保留在 summary。全篇没有合格对象时 semantic=[]，Event 仍有效。

同一动作的宣布、生效、计划执行、实际执行时间放在同一对象，不因日期不同拆对象。独立动作分别表达，不机械排列组合主体、对象和日期。政策、动作状态、确认状态相互独立；公司正式宣布建设计划可为 GENERAL/PLANNED/CONFIRMED，传闻已完成收购可为 GENERAL/OCCURRED/UNCONFIRMED。预测属于 UNCONFIRMED，不冒充执行主体承诺。

示例：9月11日公布终裁，措施10月1日生效。整篇一个 Event；“公布终裁”对象的 announced_time=9月11日、effective_time=10月1日、其余时间=null，POLICY/OCCURRED/CONFIRMED。不得制造另一条“已执行”记录。

## Data 与执行边界

- Data 分配 Event/子对象/关系 ID；只校验类型、枚举、引用、0–5关键词及幂等等合同，不进行原文语义判断。
- event_evidence_links 使用 raw_evidence_id 外键，去除 contribution_weight；event_id/raw_evidence_id 各自唯一。Event 与 Raw 关系原子提交，不允许无来源的 Event。
- 删除 event_actor_links/event_asset_links；不等于删除图谱中故事线及 Signal 关联。
- 相同发布键同载荷重放返回原 ID，不更新时间，不重复生成子对象；不同载荷冲突。对同一 Raw 的主动重提取不通过更换发布键制造第二条 Event，需另定显式修订合同。
- 输入以冻结的完整文档为依据。缺正文或截断不能宣称完成全文提炼。单篇失败留存状态，不丢弃其他文档；发布与入图恢复不得重新触发已成功提取。
- 旧 Evidence 队列/旧原子 Event 不可重新标记成新数据；迁移在历史处理未确认时必须拒绝，而非隐式清库或合成时间。
- 删除 evidences 需要同步解决报告引用；最终报告引用目标待用户确认，不以 Raw ID 冒充 EVD ID。旧报告不可被静默改写。

## 验收例

1. 多个日期的一篇文章仍为一个 Event；同一事项的四种时间按原文分别保留。
2. 没有任何明确时间仍生成 Event，semantic=[]。
3. 指标超过五项只选五条 keywords，summary 保留其余关键信息。
4. 计划日期已过仍不自动填写 executed_time；四类时间允许模糊原文文本。
5. 同键重放不产生新 Event/子对象/来源关系，不改 collected_at；同一 Raw 的不同键不得产生第二个 Event。
6. 来源不存在、枚举非法、关键词超过五条明确失败；不靠截断、猜测、虚构 ID 或回填假时间修复。
7. 新旧合同不可混用；移除旧表前验证所有引用和消费者，实际环境执行需单独确认。
