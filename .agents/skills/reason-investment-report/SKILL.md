---
name: reason-investment-report
description: 使用 Codex 分析师、AgentOS 数据 CLI 和冻结数据，执行地缘政治、宏观经济、产业链三路独立基本面推理，生成可审阅的结构化投研报告。用于完整报告生成或重新推理；HTML 预览和 Data Service 发布是独立步骤。
---

# Codex 三路独立投研报告

Codex 完成证据阅读、变量综合、基本面分析、传导判断、报告撰写和语义审查。CLI 只查询、冻结和校验，不调用模型或替分析师生成结论。执行代码复用 `capabilities/report_reasoning/`，不修改 `capabilities/investment/`。

## 读取合同

从本 Skill 所在仓库根读取：

- [数据工具与运行边界](../../../capabilities/report_reasoning/README.md)：取数前读取。
- [分析工序](../../../capabilities/report_reasoning/ANALYST.md) 与 [变量综合方法](../../../capabilities/report_reasoning/VARIABLE-METHOD.md)：每个分析上下文均读取。
- [变量综合报告合同](../../../capabilities/report_reasoning/internal/variable-report.schema.json)：写报告及校验时读取。

当前变量综合产物是 `report-publication/v6-draft`；旧文档中的 v5 生成说明不适用于本模式。不把草案称为现有 Data Service 已支持的发布格式。

## 必读方法与迁移合同

本文件是入口，不是全部方法。开始对应工序前必须读取以下配套规则；每个独立分析上下文也须读取数据、推理、变量与报告四份文件，不依赖本任务聊天历史。

- [取数合同](references/data-contract.md)：Event/Fact Signal 范围、来源闭包、多分类、分页、时间口径、图谱扩展及当前 CLI 限制。
- [三路推理指南](references/reasoning.md)：地缘、宏观、产业与公司分别如何形成基本面结论。
- [变量与传导](references/variables-and-transmission.md)：合并、冲突、适用性、直接/推理、方向、影响度和置信度。
- [报告与文案合同](references/report-contract.md)：字段归属、总结详情一致、正式名称、类型、中文方向和呈现规则。
- [执行与审阅](references/execution-and-review.md)：每道工序输入输出、独立性、恢复、验收及后续固定流程迁移边界。

下文为执行摘要。详细规则不能跳过；旧 ANALYST.md 中关于 v5 或逐条信号展示的描述以变量综合合同及本 Skill 配套规则为准。数据来源查询与报告 JSON 形状仍以已实现 CLI 和 Schema 为技术真源，遇到不支持的业务口径应阻断并记录，不能声称已经支持。

## 1. 核验目标并冻结数据

明确用户选择的环境、分析时间范围、截止时间，以及全量历史还是增量口径；不能从旧报告继承隐含时间范围。Event 的未来生效时间不等于采集时间或报告运行时间。实时 CLI 用 `--time-field created_at` 筛选 Event 入图时间，用 `--time-field valid_at` 筛选生效时间（默认）；记录 `selection_time_field`，不能静默换用另一口径。入图时间不是新闻发布时间或 Data Service 入库时间。

UAT 使用用户已有 SSH 连接远程执行数据 CLI；用户通过 NVIDIA Sync 开通连接时，不主动创建隧道。不将本机 CLI 所在位置当作数据环境证明。

远程读取前核对 SSH 目标主机、实际 hostname、UAT Compose/容器身份、代码或镜像版本、Event 数据目录及 CLI 可用性。`RUNTIME_ENV=prd` 本身不能证明是 UAT。只输出必要身份信息，不输出数据库密码或完整环境变量。

连接失败、身份不符、缺少 CLI、来源未完成或格式不支持时，记录阻塞并停止正式取数；不回退本机、旧报告或旧快照。API 适配尚未实现，SSH 端口转发本身不能替代 CLI 所需的远程 journal 读取。可做只读单条诊断，但不得将它冒充完整取数验收。

在目标运行环境执行 README 中 `prepare --live`，使用独立新目录；不覆盖历史报告。将生成的快照和 manifest 取回本机，核对哈希。记录 `target_environment`、SSH 目标、hostname、容器、代码/镜像版本、远端数据路径、查询时间与筛选口径、快照哈希。远端只运行数据工具，不需要 Codex CLI。

## 2. 三次独立分析

本流程采用三个独立分析上下文，可使用三个 Codex 子代理；各自只收到工序合同、同一冻结输入的位置、自己的输出目录与范围，不能收到旧报告或兄弟工序的结论。主上下文负责合并和完整性审查，不将一路结论送给另一路。没有独立上下文能力时明确未满足独立性，不能用同一上下文顺序作答冒充独立推理。

| 工序 | 原始输入 | 总结中的受影响锚点 |
| --- | --- | --- |
| 地缘政治 | GEOPOLITICAL Event 及相关 Signal/Evidence | 宏观故事线、产业链 |
| 宏观经济 | MACRO_ECONOMIC Event 及相关 Signal/Evidence | 产业链 |
| 产业链 | GEOPOLITICAL、MACRO_ECONOMIC、INDUSTRY_CHAIN、COMPANY 全部 Event 及相关 Signal/Evidence | 产业链节点 |

逐页查询直至 `next_offset` 为空，核对总数和唯一 ID；所有 Event 都需分析，包括没有 Signal 的 Event。真实实体目录和图谱结构可查询全目录，但候选关系不是影响证据。

每路先按实体、变量和可比范围综合信号，再形成基本面影响与升温/降温/分化判断。根据时间、实际发生或计划、产品地域范围、来源可靠性和经济机制处理冲突，不投票、不机械加权；不同范围不能强行抵消。原始 Signal 保留原文、方向及引用，综合方向不能覆盖源方向。

自身信号必须有直接判断，包括错配、失效后的“证据不足以判定”；可成立的上下游及上下层传导必须判断并标记推理。说明需求、供给、成本、利润或现金流机制，变量上升不等于投资升温。公司信号参与公司→节点→产业链传导；产业链工序在本上下文独立分析地缘→宏观→链→节点。无法推导的节点不进入报告；链图仅保留有判断节点之间的真实边，不跨越被移除节点补造边。

各路保存自己的 `report.json`、`audit.json`、`review.json`。audit 对每个 Event/Signal 记录使用、限定或排除的理由及结果位置；产业上层变量判断可保存在内部审计。audit 不进入发布报告正文。

## 3. 组织结构化报告

- 标题与实体名称只使用冻结目录正式名称；不拼接判断摘要，不把结论当作产业链标题。判断放到 `summary.conclusion` 或实体 `assessment`。
- 一句话结论直接陈述，不加“待确认：”“待评估：”等状态前缀；保留“不足以判定”等必要证据边界。
- 文案中的方向用上升、下降、平稳、分化、未明确，不写 UP/DOWN/UNKNOWN 或 unknow。结构化方向枚举保持合同值，原始来源文字不篡改。
- 类型单独展示，不能拼进名称。总结使用 `affected_refs[].target_type`；详情按实体所在结构识别宏观经济、产业链、产业链节点或公司。
- 每个实体分别保存自己的 `variable_signals` 和 `variable_assessments`；多个宏观、链、节点、公司不共享父级变量表。
- 同变量可比范围合并，不可比范围明确 `scope/timeframe`；支持、反向、排除三组互斥且完整覆盖自身原始信号，保留全部 Evidence 引用。
- 地缘及宏观中的链详情与产业链工序使用同一链/节点字段结构。链级推导逻辑、支持、反证综合表达上下层因素，不逐层堆叠章节。
- 总结和详情的传导逻辑使用简短箭头因果链；影响度、方向、置信度分开，限制条件与反证不能省略。

## 4. 合并、检查、完成

各路独立完成并审查后，主上下文合并已完成部分，不引入新推理。发现语义缺口时将问题交回所属工序，不泄露其他工序结论。

执行 `python -m capabilities.report_reasoning validate --run INPUT --report REPORT --receipt RECEIPT`。另外审查：全量 Event/Signal 覆盖、变量冲突处理、无依据传导、遗漏直接判断、正式名称、独立类型、总结详情一致性、Evidence 回溯和三路独立性。结构校验通过不能替代语义审查。

每次输出目录保存输入来源记录、快照或可验证引用、三路产物与审计、最终 report.json、校验结果及运行状态。任何后续文案修改都同步 JSON、重新校验并更新哈希；原始分路产物可保留，但记录最终编辑差异，避免旧校验凭据指向新数据。

只有三路完成、语义审查和结构/来源/覆盖检查通过，才标记 completed。交付结构化报告路径、输入环境与范围、关键限制和未发布状态。HTML 是用户需要时单独调用 `tools.render` 的预览步骤；Data Service 发布和定时任务创建均不属于本 Skill 默认动作。

## 发布显示名称

推理仍使用正式名称。制作发布数据包时必须执行[发布名称投影](references/publication-names.md)：四类实体使用图谱 short_name 作为既有 name/title 字段值，所有 ID 与引用保持不变。
