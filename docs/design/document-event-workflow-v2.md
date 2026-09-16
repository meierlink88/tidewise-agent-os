# 事件提取：文档级候选工作流

Issue #266。新增 `event-extraction-v2`，中文名“事件提取”；不改旧 `event-extraction`、采集工作流及 Schedule。无默认定时任务。本轮终点是判重后的候选，不是正式 Data Event 或故事线/Signal。

## 画布与执行

准备数据（Function） → Loop（最多20轮，每轮一个 Event 提取 Function） → 汇总（Function）。
条件、两个 Agent 调用、向量召回及副作用都在 Function 内封装。准备阶段选取最多20个尚未处理的 Raw article_key，读取并冻结完整已归档文档；不足20则处理实际数量，无数据直接结束。不读取旧 Evidence 表。

原文来自 Collection V2 成功归档目录，经 collection_v2 公共接口校验正文/Markdown 哈希与归档回执。正文按归档实际内容完整传入，不截断、不额外抓网页，不把提供方摘要冒充全文。以 article_key 识别 Raw 版本，此阶段不伪造正式 RAW 或 EVT ID。

每篇流程：

1. 原文事件提取 Agent：一篇一个 title、summary、semantic 数组和0–5个量化keywords。
2. 固定输入 `title.strip() + "\n\n" + summary.strip()`，复用 Graphiti embedder 生成向量。
3. Neo4j 向量索引召回相近正式 Event 和之前保留的候选；过滤自身、业务分组、模型与输入版本，最多30条。
4. 有候选时调用事件重复判断 Agent；无候选直接保留。重复必须指向本次召回中的候选ID；无法确定时保留，不用分数阈值自动删除。
5. 重复仅记审计，不进入待发布区；非重复保存为独立 `EventExtractionCandidate` 节点及本地文档候选。两者都保存源 article_key、判定或错误阶段。

Agent 规则：summary忠实保留原文主体、动作、对象、量化指标、口径、条件、否定和不确定性。semantic对象包含actor/action/target、announced_time/effective_time/planned_execution_time/executed_time文本字段，以及POLICY/GENERAL、PLANNED/OCCURRED、CONFIRMED/UNCONFIRMED；无position。动作明确、主体/对象至少一个可识别、至少一类时间有原文依据才提取；没有符合条件的事项返回空数组。该成立规则由模型遵守，代码不做原文语义硬判断或任意文本长度校验。

将“计划”写成“已执行”、不同财报期间、数字口径变化、传闻变确认、有新增重要事实等均不能因主题相似而丢弃。只合并整篇核心事实重复、无新重要信息的报道。最多5条keywords是用户明确的数据合同。

## 向量与历史

不修改 Graphiti 库。`EventVector` 是向量检索标签，不代表正式Event；已有兼容向量的正式Episode可参与召回，待发布候选使用 `EventExtractionCandidate`，不会出现在旧 Episode/Signal 查询中。

`event_embedding` 存向量；hash保存输入文本SHA-256；model/version记录模型及 `title-summary.v1`。索引 `document_event_embedding_v1` 使用 cosine；维度来自既有embedding配置，不硬编码阈值。所有检索记录必须使用同一模型和输入规则。

历史Event不要求保留或迁移，本轮不提供历史向量补齐流程，也不以历史向量完整性作为运行前提。已有兼容向量的记录可以参与召回；无向量的旧Event不参与向量判重。新工作流自身产生的候选必须携带兼容向量，保障当前批次及后续批次之间的判重。不会主动删除历史业务数据。

向量是近似召回，模型仅比较被召回的候选，不能声称全历史零漏判。现阶段不接入全文召回，也不套用七天窗口。

## 失败、并发与恢复

使用独立 `data/event_v2`（可用 `EVENT_V2_ARTIFACT_ROOT` 指定），不污染旧 Event journal。

- 按article_key冻结Event、向量、判重结果和终态。正式发布时间复制原文，可空；collected_at为首次成功提取完成时间，重试不变。
- 同一共享数据卷上，以文件锁串行化单篇召回/判定/候选写入；多个运行选到同一原文时只有一个处理，后者记录already_processed。多主机必须共享同一协调存储，本实现不声称跨独立磁盘的分布式锁。
- 普通单篇模型/向量/源文件问题记录失败并继续；磁盘读写等无法安全记录的故障终止，不报告完成。
- 失败项默认不反复抢占下一批；手动输入 `{"retry_failed":true}` 可重试，复用已经冻结的提取与判断。候选入图回执丢失时按稳定ID/载荷重放。
- 丢弃重复指退出待发布候选，不删除 Raw 或审计文件。
- 输出区分completed/partial/failed/no_change，并给出accepted/duplicates/failed/already_processed。Agno技术COMPLETED不代替业务结果判断。

## 交付与回滚

新增两个独立Studio Agent及工作流，代码仅seed缺失组件，保留人工发布配置。无新工具权限、Schedule或Data发布。回滚可停用新工作流、回退本PR；保留候选与审计待明确处置，不能删除共享Neo4j卷。后续正式发布必须另行设计 Raw正式登记、候选到Event映射及失败补偿。

## 本地验证（2026-09-16）

- 6项自动化场景测试通过，包含20篇上限、失败继续、重试、并发及隔离REST/MCP。
- 实际模型、Embedding及Neo4j隔离样例：2篇同事件报道，accepted=1、duplicates=1、failed=0；测试候选随后清理。宣布时间与生效时间分列、keywords仅保留量化事实。
- 本地health、Agent REST及平台MCP冒烟通过，新工作流可见；旧workflow发布版本仍为39/2/16/29/16。
- 新增模块显式mypy检查通过；全库ruff检查和格式检查通过。全库mypy的既存依赖/API类型错误见PR验证说明。
