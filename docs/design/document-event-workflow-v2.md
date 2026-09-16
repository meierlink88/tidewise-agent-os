# 事件提取：事件分析师与分步 Skill

Issue #266，Workflow `event-extraction-v2`，中文名“事件提取”。保留旧 `event-extraction`、其他 Agent、采集及 Schedule。本轮先搭建四步框架，只实现事件提取 Skill；后续三个 Skill 逐一完善。

## 编排

准备数据（最多20篇已归档Raw）→ Loop → 汇总。

Loop 对每篇文章依次执行四个 Function Step：

1. **事件提取**：一次调用事件分析师，由其提炼、调用相似事件检索工具，再判断重复，统一返回 Event 和去重结论。
2. **故事线关联**：未实现，占位返回 `not_implemented`。
3. **变量信号发现**：未实现，因前序未完成返回 `blocked`。
4. **数据发布**：未实现，因前序未完成返回 `blocked`；记录本篇框架结果，再推进下一篇。

重复、失败、已处理文章的后三步返回 `skipped`，不调用 Agent 或外部写入。只有最后一步才能推进文章游标，顺序错误拒绝执行；重复进入提取步骤复用当前文章状态。

输出区分 `extraction_outcome` 和整体 `outcome`：存在保留候选时整体为 `incomplete`，且 `pipeline_completed=false`、`published=false`，列出三个 `pending_steps`。Agno 技术完成不表示业务发布完成。

## 单 Agent 与 Skill

本轮新流程只注册一个代码定义的 Agent：`event-analyst`，中文名“事件分析师”。统一使用 `default_model()`、`get_postgres_db()`；通过 AgentOS 和 Registry 注册，可通过 REST/MCP 调用。旧流程的 Agent 保持不变。

角色职责描述覆盖事件提取、故事线关联入图、信号发现入图及 Data Service 发布；当前开放能力仅为 `skills/document-event-extraction/SKILL.md`。不创建后三个空 Skill，避免模型误以为能力已可执行。

业务提炼和判重规则只在 Skill 管理。Function 每次从注册 Agent 创建独立执行实例，配置本步 Skill、输出结构和允许的工具；不修改共享 Agent。返回 `DocumentEventAnalysis`，包含 `event: DocumentEventDraft` 与 `deduplication: DuplicateDecision`。不再由 Function 分别编排提炼和判重模型调用。

使用 Agno `Skills/LocalSkills` 原生加载，同时将当前 Skill 完整正文注入本次指令，保证固定步骤无需依赖模型主动读取规则。单独聊天时 Agent 可以通过 `get_skill_instructions` 读取 Skill。每篇配置一个只读 `search_similar_events(title, summary)` 工具（`capabilities/event_v2/tools/search.py`），封装向量化和候选召回；候选写入仍由 Function 执行。注册 Agent 不持有共享检索状态，独立聊天未绑定本步骤工具时不能完成去重。

每次调用使用独立文章 session，不读取历史对话。批次记录 Agent 代码合同版本与 Skill 内容摘要，运行中规则变更须新开批次，避免同一批次规则漂移。

## 提炼合同

一篇原文输出一个文档级 Event，包括事实 title、忠实 summary、semantic 数组及0–5条量化 keywords。不按原子事实拆分多条 Event，不做投资推理。

semantic 对象包含 actor/action/target；announced_time/effective_time/planned_execution_time/executed_time 原文时间文本；statement_type(POLICY/GENERAL)、action_status(PLANNED/OCCURRED)、assertion_status(CONFIRMED/UNCONFIRMED)。无 position。对象成立条件由 Skill 指导，不额外增加原文语义或任意文本长度硬校验。

summary 保留主体、动作、对象、数字、口径、期间、条件及不确定性。keywords 每条须为含明确数字指标的事实短语，不能是主题、主体名、日期或编号。原文没有合格量化事实时返回空列表。

## 输入、判重与存储

Collection V2 公共接口列出成功归档 article_key，最多读取20篇，冻结归档正文并校验原文/Markdown哈希及归档身份。不截断、不补抓网页、不把提供方摘要冒充完整网页。

以 `title.strip() + "\n\n" + summary.strip()` 生成向量，复用 Graphiti embedder；Neo4j `document_event_embedding_v1` cosine 索引召回最多30条兼容候选。模型判断整篇核心事实重复且没有新增重要信息才丢弃；候选引用必须来自本次召回。无法确认则保留。

非重复保存为 `EventExtractionCandidate:EventVector` 及本地候选，保存 Raw article_key，不伪造正式 EVT/RAW ID。采集完成时间在首次成功提取时冻结，新闻发布时间复制原文。此阶段不创建故事线关系、信号或正式 Data Event。

历史 Event 可丢失，不补齐历史向量，不以历史完整性阻塞运行；已有兼容向量的记录可参与召回，无向量旧记录不参与。不主动删除历史业务数据。近似召回不保证全历史零漏判。

## 恢复与边界

独立 `data/event_v2` 保存批次原文及单篇原子 analysis 检查点，后者绑定 Event、判断、成功检索参数、向量、候选及版本；Event/recall/decision 是可重建审计文件。新调用不复用旧版零散提炼或判重缓存，已完成文章仍跳过。单篇模型错误记录后继续；不能安全保存进度的磁盘错误终止。显式 `{"retry_failed":true}` 可重试失败提取并复用检查点。

共享数据卷文件锁协调并发判重和候选写入；不声称支持独立磁盘的多主机锁。后续启用后三步时，需要接入候选续处理、稳定正式ID、图谱写入和Data发布幂等回执；本轮不假装已实现。

现有 v1 文档提取框架自动升级为 v2 四步框架，之后启动不覆盖已发布 v2 配置。本地此前注册的 document-event-extractor/document-event-identity 已软归档，保留历史审计；仅事件分析师作为新流程入口。

回滚：停用新工作流、回退代码。保留候选及审计，不删除共享Neo4j卷，不改变旧 Event 工作流。

## 验证

9项自动化测试覆盖批次上限、重复和失败跳过、检查点、并发、原生Workflow/Studio往返、隔离REST/MCP、单Agent工具配置、检索失败与最终文本一致性、上下文隔离及四步推进顺序；模型部分使用替身，不处理真实文章。

按用户要求，本轮真实模型工作流验证仅使用一篇隔离样例。验证日志与PR记录实际结果；不执行20篇真实批次。全库ruff、格式及新增模块mypy检查，旧测试脚本的本地MCP依赖兼容问题单列披露。

## 工具执行保障

工具使用每次 Agent 调用独有的实例，由代码绑定 article_key。成功调用才保存回执；再次调用前清除旧回执，失败不能沿用先前候选。返回后检查最终 title/summary 与成功检索参数相同，重复 matched_id 来自本次候选。未调用、检索失败、修改文本后未重查均记录本篇失败，不保存候选。空候选是成功结果，仍由 Agent 返回非重复结论。

Agno 集成使用原生异步 Python 函数作为 Tool，无新增依赖或密钥：[官方工具文档](https://docs.agno.com/tools/creating-tools/overview)。
