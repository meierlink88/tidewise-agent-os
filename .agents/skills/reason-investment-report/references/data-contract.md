# 取数合同

## 运行范围与来源

每次先写 source-manifest.json：目标环境、SSH 别名、实际主机、Compose/容器、运行 release SHA/镜像、Event journal 路径、查询时间、截止时间、全量/增量、时间字段及边界、CLI 版本。记录实际核验值，不把示例 dgx-spark 当作永久身份。仅通过用户已有连接访问；无连接或身份不符时停止，不新增隧道、不回退本地。

当前实时 CLI 在 AgentOS 运行环境读取 Neo4j 和 Event journal，不是 HTTP 客户端。Event 来自 Episodic EVENT，分类和 Evidence 关联来自 journal，Evidence 内容来自批次 input.json；Signal 来自 SIGNAL_ON。实体只取真实 ID、名称与类型，结构只取关系，不把实体历史摘要带入本次事实输入。

当前 `prepare --live` 筛选 `start <= Event.valid_at <= end`，end 不允许未来。它不支持按采集/入库时间取增量，不能仅凭更改参数名称实现。全量截至目前已知事实包含未来生效计划时，此筛选可能漏掉计划 Event；必须记录这一缺口并补齐能力后再宣称全量。旧导出里 Event 最早/最晚生效时间也不是报告分析窗口。

用户未指定时间口径时先明确；可以并行核验环境及只读盘点，但不擅自冻结缩减后的正式范围。不能以减少报告篇幅为由缩小数据范围。

## 三路 Event 和 Fact Signal 集合

设共同冻结 Event 集合为 E，每条 Event 可有多个分类。

| 工序 | Event 集合 | 信号候选 |
| --- | --- | --- |
| 地缘 geopolitics | E 中 classes 含 GEOPOLITICAL | source_event_ids 与该集合有交集的全部 SIGNAL_ON |
| 宏观 macroeconomics | E 中 classes 含 MACRO_ECONOMIC | source_event_ids 与该集合有交集的全部 SIGNAL_ON |
| 产业 industry | E 中 classes 含 GEOPOLITICAL、MACRO_ECONOMIC、INDUSTRY_CHAIN、COMPANY 任一项 | source_event_ids 与该集合有交集的全部 SIGNAL_ON |

规则：

1. 先定 Event，再取关联 Signal；不能按信号锚点类型、变量方向或是否容易形成结论筛选。地缘 Event 自身关联到公司/宏观/节点的信号仍在地缘工序内。
2. 同一 Event 多分类可进入多路，各路独立分析，不是跨路污染。同一路按 Event ID 去重，保留其分类与全部 Evidence。
3. Signal 的所有来源 Event 必须在本路范围内，才可把其完整原文作为本路事实使用。当前 CLI 对跨窗口来源缺失报 `Signal source closure missing`，对跨工序混合来源报 `mixed-scope Signal requires source decomposition`。
4. 多来源但均在本路范围内时，一条 Signal 保留全部来源，不复制成多条。跨范围混合信号需要来源级事实分解，当前 CLI 不支持自动完成；停止受影响工序，保留 ID 和缺口，不能删除越界 ID 后继续使用原文，也不能把其他类 Event 偷加进来。
5. 同向、反向、平稳、未明确、过期、预期及错挂信号均先完整读取，再作适用性判断。没有信号的 Event 也必须审阅。
6. 查询目标锚点结构时，不自动取入该实体全部历史事件/信号。每次 `signals --id` 仍在固定工序范围内。范围扩展必须创建新输入版本，而非中途混入新事实。

## Evidence 与完整性

Event→Evidence 引用必须闭合；Signal 的 Evidence 集合由其来源 Event 的 Evidence 合并而来，因此“存在引用”不代表每个 Evidence 都直接支持信号中的每句描述。分析师必须逐条阅读并确认具体支持点。

CLI 提供 Evidence 的 title/summary/semantic，不承诺完整文章原文。`variable_signals[].signal` 保留图谱信号记录的原文，不将其冒称新闻逐字引文。必要内容超出已返回材料时记录缺口；通过已授权来源补查也必须记录来源、范围与新快照版本，不能凭常识补齐。

逐页读取 events、signals、evidences，固定筛选条件，使用 next_offset 直到 null；核对 total、唯一 ID、缺失页、引用闭包。准备阶段发现重复 Signal/Event ID、缺分类、缺 Evidence、未知分类、选中 Event 未完成时失败，不把异常当作空数据。来源变化导致捕获哈希不一致时，等待数据稳定后重新取数，不拼接两次结果。

当前 drift gate 是两次图读取和 journal 文件字节比较，不是数据库事务级跨系统快照；也不证明全采集队列已完成。保持这一验收表述边界。

## 实体与关系查询

所有产品实体需真实 ID、正式名称和类型。以 ID 连接，名称搜索仅用于找候选；同名不同公司不能替换。Signal source 是 Variable，target 是自身锚点。IndustryChain 是分析视图，不能将其成员信号直接归成链自身 Fact Signal。

可查全目录中的实体身份与结构：ChainNodeBelongsToIndustryChain、CompanyParticipatesInChainNode、ChainNodeInputTo、ChainNodeIsComponentOf、ChainNodeDependsOn。节点可属于多条链，不能强制唯一归属。

从自身信号锚点及无信号 Event 的真实相关候选开始，逐跳查相关上下游/公司业务/链成员；每一跳均需判断机制和业务敞口，不能只按拓扑漫游。已有事实不足、无新增机制、循环回到已判断对象或对象未能真实定位时停止该路径并记录原因。不同路径可对同一实体产生相反作用，要综合，不无穷展开。

当前图结构不提供完备的地缘→宏观→产业因果边；这些是 Codex 条件性经济推理，必须写依据，不能伪装成图谱事实。市场 Concept 主数据及其关系不在当前 CLI 完整输出中，不据名称补造 Concept 分析。
