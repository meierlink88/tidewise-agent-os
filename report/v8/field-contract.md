# v8 报告字段合同 · 审核稿

本稿沿用 v7 冻结输入，报告结构暂标 `report-publication/v5-draft`，不表示 Data Service 已支持。`report.json` 是内容真源，`report.md` 与 `report.html` 从同一 JSON 生成；`report.schema.json` 与 `scripts/validate_v8.py` 联合校验形状和跨字段关系。

## 判断与可见性

- 每个单元、宏观对象、产业链、节点及公司对象增加 `judgment_origin`，值为 `direct`（直接）或 `inferred`（推理）。
- 直接：本实体在该报告层允许的 Event 输入范围内存在经审查可采用的变量信号。直接不等于来源已独立核实、结论必然实现或 Signal 本身正确无误。
- 推理：通过相关 Event 或其他锚点形成有条件判断；既有正式图边只用于表达结构，不自动证明影响传播。
- 不强制所有判断具有上涨或下跌方向。具备直接信号但只支持规模、份额或未知方向时，应明确判断边界，不凭空补增长结论。这类判断仍为“直接”，不是未评估。
- 图中 `graph.nodes` 与 `affected_nodes` 一一对应，`graph.scope=assessed_nodes_only`。过滤后不重连被移除节点两端；`empty_state` 暂仅保留 null 以便对照 v7。
- 无法形成判断的节点从报告移除，原因仅在内部覆盖审计中保留。

## 变量信号的位置

| 归属对象 | 新增字段位置 | 页面位置 |
| --- | --- | --- |
| 地缘／宏观故事线自身 | `detail.variable_signals` | 详情开头；不新增“故事线推理结果”章节 |
| 宏观影响锚点 | `detail.macro_impacts[].variable_signals` | 对应对象判断之后、反证之前 |
| 产业链影响锚点 | `detail.industry_chains[].variable_signals` | 链级判断之后、链级推理总结之前 |
| 节点影响锚点 | `detail.industry_chains[].affected_nodes[].variable_signals` | 对应节点判断之后、反证之前 |
| 有可靠业务传导的公司 | `detail.companies[].variable_signals` | 当前产业链分析单元的相关公司判断内 |
| 无可靠产业链归属的公司 | `company_analyses[].variable_signals` | 产业链层末尾的公司直接判断内 |

多个对象各自保存独立数组，数组为空时不展示空表。页面固定三列“变量、方向、信号”。方向由 source_direction 映射为上升、下降、不变、分化、未明确；信号栏仅呈现冻结 Signal 的 fact 原文，适用限定和溯源放入展开区域。Signal 永远归属其冻结目标实体；例如公司信号不能复制进节点的变量数组。公司与节点通过 `reasoning_sources.upstream_refs` 表达有条件的业务传导。

## Signal 行合同

`variable_id`（冻结 Variable UUID，无独立业务 ID 时不伪造）、`variable_name`、`signal_id`、`signal`（冻结 Signal 的 fact 原文）、`source_direction`（原始方向）、`adoption`（adopted/qualified）、`qualification`、`event_ids`、`evidence_ids` 均必需。

来源文本原样展示，方向为原始信号方向，不是报告采纳后的结论方向。适用限定单独保留并可展开查看；判断正文仍遵守限定：人形机器人出货规模不作为已确认需求增长，清障活动不作为通道已经恢复。排除信号不进入表格，源 Event 如仍有适当用途可在推理中有条件引用。

## 推理来源

每个判断对象增加 `reasoning_sources`：

- `signal_ids`：该对象直接采用的自身信号，与其变量表一致；传入的公司信号不列作节点自身信号。
- `event_ids`：本判断允许使用的相关冻结 Event。
- `upstream_refs`：引用同一分析单元的 `entity_id` 与 `local_key`；跨公司业务传导另外填写 `mechanism` 和 `condition`。

总结仍通过 `affected_refs` 引用详情判断，不另存一份方向或标签。`assessment` 的结论、范围、周期、条件、置信度与 Evidence 沿用 v7 字段。

## 补齐漏覆盖对象的承载结构

保留原 `geopolitical_stories`、`macroeconomic_stories`、`concept_analyses`。

- 新增 `industry_chain_analyses`：与 Unit 同形状，`source_id` 为真实 ICH。用于有可用信号却无冻结 Concept 映射的产业链；不伪造 Concept。当前补充炼油及石油化工、煤炭开采、航空运输服务、火力发电设备四个分组。
- 新增 `company_analyses`：与公司判断对象同形状。用于直接信号有效、但业务到链节点的归属尚不能可靠成立的公司。它们仍属于产业链报告层，不另设第四层报告。
- `detail.companies`：公司判断对象数组。共同使用 `local_key/source_id/name/assessment/variable_signals/objections/judgment_origin/reasoning_sources`。

以上两类承载结构是本次审核稿的新提议，需要用户看过效果后再进入服务合同；不能把它们描述成 v7 已有结构。

## 输入范围与版本

地缘仅使用地缘归类 Event 与相关 Signal；宏观仅使用宏观归类 Event 与相关 Signal；产业链层使用三类及公司 Event 与相关 Signal。冻结 168 Event、77 Signal 未刷新。本次通过既有证据和明确业务机制补充报告判断，不是重新运行原生 Workflow。

Data Service 尚未修改；当前稿不可直接发布到 v4 接口。审核后再确定正式发布版本、读取 DTO、Signal/Event/Evidence 校验、分页及兼容策略。
