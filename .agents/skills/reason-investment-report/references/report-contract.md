# 报告合同与文案

字段名称、必填性、可空值、枚举和引用形状以 `capabilities/report_reasoning/internal/variable-report.schema.json` 为准；此文解释字段含义，不另创一份冲突 Schema。当前是 v6-draft，不能称现有 Data Service 可直接接收。

## 结构与归属

| 对象 | 所在结构 | 要求 |
| --- | --- | --- |
| 地缘总结 | geopolitical_stories[].summary | 根为真实地缘故事线；受影响引用为宏观或产业链 |
| 宏观总结 | macroeconomic_stories[].summary | 根为真实宏观故事线；受影响引用为产业链 |
| 产业总结 | industry_chain_analyses[].summary | 根为真实产业链；受影响引用为该链节点 |
| 故事线变量 | unit.detail.variable_signals / variable_assessments | 只放该根自己的信号及综合判断 |
| 受影响宏观 | unit.detail.macro_impacts[] | 每个宏观实体分别拥有完整 assessment、变量、反证和来源 |
| 受影响产业链 | unit.detail.industry_chains[] | 每条链分别拥有链级判断、reasoning_summary、graph、affected_nodes、变量及来源 |
| 受影响节点 | chain.affected_nodes[] | 每个节点完整 assessment、reasoning_sources、变量及反证，不能少字段 |
| 公司 | unit.detail.companies[] 或 company_analyses[] | 本公司变量和判断；解释业务挂接或不能挂接的原因，不创造第四类总结 |

地缘、宏观详情中的链/节点与产业工序采用同一合同，不能因上层类型而省去判断、周期、条件、反证或来源。链级推导逻辑、支持、反证综合表达，不按地缘/宏观/产业/公司拆成多套层级。

每个实体使用真实 source_id；local_key 只在报告内定位，可在不同工序为同一真实实体使用不同局部键。跨工序同实体方向不同不自动判错，因为输入范围不同，不能合并成统一结论。上下游引用需满足现有校验器的单元内闭包；产业内部上层审计不是可直接跨单元引用的产品对象。

总结 affected_refs 的 target_type 枚举为 macroeconomic_story、industry_chain、industry_chain_node。类型应是独立字段/显示位置，不能拼入 name/title。详情按 macro_impacts、industry_chains、affected_nodes、companies 的结构位置识别类型，不给 Schema 不允许的对象强加 target_type。

图中节点与 affected_nodes 一一对应，节点必须真实属于该链，边及方向必须来自结构。去掉不可评估节点时不把两端重新连线。不展示“暂无评估”的占位节点，也不展示无节点的空链。

## 文字职责

- title/name：正式名称，不追加判断、冒号后缀、括号中的状态或“某某受益链”等临时名称。
- summary.conclusion：直接回答本期发生什么基本面变化及主要范围，优先一句清楚的话。不得用“待确认：”“待评估：”标签开头，不重复标题。必要条件直接写入句子。
- summary.transmission_logic / assessment.transmission_logic：简短箭头因果链。示例“通道受阻 → 原油运输成本上升 → 炼厂成本承压”；每个箭头应有可说明的机制，不能堆砌事件名或默认影响已落地。
- assessment.conclusion：针对本实体说明影响、范围和关键条件；不同节点不能复制同一句链级结论。
- scope、forecast_window、conditions、follow_up：分别表达对象范围、时间、成立条件、下一步验证线索，不能用“详见上文”代替。
- impact_assessment.rationale：解释影响幅度、覆盖和持续性，不用证据条数当评分。
- variable_assessments.synthesis：变量层面的综合变化，不能直接等同产业升温；conflict_resolution 解释冲突和排除理由。

生成文案中的方向用中文“上升、下降、平稳、分化、未明确”。方向字段仍用机器枚举；原始来源文字不篡改。不存在独立的“待确认”产品分类，“证据不足以判定”属于已经完成的判断，可出现在结论中。

保留正式英文公司名和技术名，中文化规则不意味着翻译或改名全部实体。禁止为了口语流畅改写 source_id、Signal 原文或 Evidence 身份。

## 变量展示

变量放在详情各自实体层级。默认展示“变量 / 综合方向 / 综合判断”，每个可比变量范围一行；同变量多个范围必须同时显示 scope/timeframe。展开支持、反向或冲突、不适用三类证据，每类原始表为“变量 / 原始方向 / 来源记载”，保留来源限定及 Event/Signal/Evidence 引用。

原始信号可以在不同独立工序的自身实体处重复出现；这是各自范围的结果，不是同一路变量重复判断。不能把节点或公司的信号上移成链的直接信号。所有有范围归属的信号需在产品或内部审计找到解释；内部覆盖审计不进入发布产品。

HTML 渲染仅表达 JSON，不推理、不补字段、不临时重写结论。文案调整须先修改结构化报告，再验证和重渲染；保护旧文件，记录版本与哈希。

## 发布名称例外

上述正式名称规则适用于分析原件。发布包按[发布名称投影](publication-names.md)使用四类实体简称作为 name/title 值；字段名、身份、正文和引用不变。
