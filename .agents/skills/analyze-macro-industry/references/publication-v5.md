> 历史归档：本 Skill 已改名为宏观经济与产业链分析，当前发布仅使用 publication-v6.md。以下记录不得作为新批次执行步骤。

# 可选发布：v6 分析原件 → v5 Data Service 发布包

仅在用户明确要求发布时执行；生成报告的固定推理工序仍不自动发布。用户已确认当前产品不展示 `variable_assessments`，因此可从已审阅的 v6-draft 原件制作 v5 发布包，无需升级 Data Service 或重新推理。此约定不授权删除其他不兼容的业务内容。

## 原件与发布包

- 保留原 `report.json`、HTML、冻结输入、三路审计及所有原件哈希。原件继续使用 `report-publication/v6-draft`，保留变量综合分析。
- 创建独立 `publication-<environment>-v5/` 目录；发布包不是覆盖原件后的新基线。保留转换规则、逐字段差异、源与目标哈希及各次失败/成功凭据。
- 从原件深拷贝，递归移除全部 `variable_assessments` 字段，包括空数组。它们只是不发布，分析仍以这些综合判断为依据。
- 发布包 `schema_version` 为 `report-publication/v5`；`report_type` 必须为 `{"code":"investment_reasoning","label":"投研推理报告"}`，不能使用分析工序自定义 code/label。
- 保留 `variable_signals`、原始方向及原文、adoption/qualification、Event/Evidence 引用、direct/inferred、影响度、结论、条件、反证、图和受影响锚点。不能将公司信号迁入节点表；`company_analyses` 和详情 `companies` 保持空数组。
- v5 要求每条链 `reasoning_summary.logic == assessment.transmission_logic`。若原件两处只是同义表述，分析师确认范围、机制、条件和方向等价后，在发布包统一为同一已有完整表述，并记录两处原文和映射。若存在实质冲突或遗漏条件，先解决报告缺陷，不能机械覆盖以绕过校验。
- `generated_at`、分析时间窗口和已批准结论不因发布时间改变。可更新发布包中仅描述技术版本/发布状态的 limitations，准确区分 v6 原件与 v5 发布包；不删改业务限制。发布成功时间以服务 receipt 为准。
- 用稳定 `publisher_report_id` 构造 `{publisher_report_id, report}`；同一次发布的请求冻结后，不因重试改变 ID、时间或内容。失败原因、修订请求及各自哈希均留存。

## 1. 核验目标与合同

使用已授权的环境连接。UAT 可通过用户已有 DGX SSH 通道，在内存中读取 AgentOS 配置的 Data Service 地址及 token；核对真实目标，不输出 token 或完整环境变量，不写入 Skill、日志或版本库。不自行新增隧道或公网入口。

读取目标版本对应的 Data Context、v5 OpenAPI/fixture 和实际校验器。当前 `report.ValidateReport` / 严格 Report JSON 解码比分析 CLI 校验更严格；不能以 CLI passed 代替服务合同验收。可在仓库外临时工具中只读复用 Data Go 校验器，记录所用 commit，避免修改产品源码。无匹配校验器时明确该检查未完成，不能伪造通过记录。

特别检查正式报告类型、未知键/非法 null、链双份逻辑一致、direct 与自身信号数量、`reasoning_sources.signal_ids` 与自身信号行的顺序、单元内上游引用闭包、条件/跟踪/置信度、图节点与判断一一对应、反证状态和 Evidence 引用。全为不适用信号的实体仍可有 direct 有界判断，不篡改来源来伪装成 adopted。

- 对已有有界传导、暂不能判断方向的 reasoning_hypothesis，按目标合同使用 pending_validation，并保留条件、跟踪项、置信度和 Evidence；不能把缺少推导依据的对象机械改状态后发布。需修正的语义或状态先回到分析原件审阅，不在发布投影中暗改。
- 图边按起点、终点、关系标签去重；同一真实关系的重复记录不是额外证据。只去重显示完全相同的边，记录差异；不补造连接、不删不同语义的边。

## 2. 冻结发布包并验证

1. 校验源报告仍匹配已审阅哈希，从源生成新包并保存 `projection-receipt.json`。
2. 执行分析 CLI 的 v5 结构/来源校验，再执行当前 Data 自身业务校验。检查前述转换白名单之外无字段改变，尤其结论、方向、身份及来源。
3. 从**发布包**递归收集唯一 Evidence ID，查询目标 Data Service 确认全量存在。不要拿整份输入或内部 audit 的 Evidence 集合代替实际发布引用。
4. 当前 Evidence 列表使用 `page_size`/`page` 分页；Report 列表使用 `limit`/`cursor`，不能混用。逐页直到引用齐全或列表耗尽，保存 available/missing 及读取目录。缺失时停止发布，不复制本地 Evidence 冒充目标存在。
5. 保存冻结的 `request.json` 和请求字节 SHA256。成功发布或不确定响应后，重试必须使用完全相同的包。

## 3. 发布与失败处理

调用目标 `POST /api/data/v1/report-publications`，使用服务认证与有限超时。

- `201`：新发布；保存 report_id、published_at、请求哈希及完整 receipt。
- `200` 且 `replayed=true`：同包重放成功，须核对原 report_id 和 published_at。
- `400/422`：合同或来源校验失败，记录响应及具体字段，查明原因后只做已授权的兼容调整，重新运行预检；不能随机删字段或改投研结论。
- `409`：相同发布身份已有不同内容，保留双方身份和哈希；不覆盖旧报告，也不盲目换 ID 制造重复版本。
- 超时/连接中断：结果不确定，使用同 ID 同包重放确认，不生成新身份。

需要代码升级时遵守 Data 仓库的 Issue→Branch→PR→人工合并→环境部署流程。已有 v5 能表达本发布包时，不为发布启动无关的 Data/Miniapp 改造。

## 4. 发布后验收

使用 Data API 本身验证，不以小程序是否展示作为服务发布成功标准。

- `/reports?limit=1`：确认最新 report_id；如并发出现更新发布，读取本次 report_id 并记录最新状态，不为抢占最新而重复发布。
- `/reports/{report_id}/home`：核对版本、类型、生成时间、分析窗口、观察和限制。
- `/reports/{report_id}/analyses/{kind}?limit=...&cursor=...`：分页核对地缘、宏观、概念聚合、无概念归属的独立产业链及空公司集合的顺序、数量和总结字段。
- `.../analyses/{kind}/{analysis_key}` 与 `.../industry-chains/{chain_key}`：逐单元核对详情、宏观影响、链、节点、原始信号、判断来源、类型及图。以实际 API 投影合同逐字段比较，不要求列表返回详情字段。
- Evidence 返回 `evidence_scope_token` 和 `evidence_count`，不是原始 `evidence_ids`。逐 scope 查询 `/reports/{report_id}/evidences?scope_token=...`，核对条数、顺序与引用内容，避免漏验或越界。
- 确认读取结果没有 `variable_assessments`，没有公司产品对象；公司作用仍保留在节点/链的结论、逻辑、支持与反证中。
- 用冻结请求重放一次，验证 `200/replayed=true`、相同 report_id/published_at（按 UTC 时刻及数据库微秒精度比较；首次响应可能含纳秒，保留两份原值，不把格式/精度差异当新增发布），保存 `replay-validation.json`。

保存 `evidence-preflight.json`、`validation.json`、`data-service-validation.json`、`receipt.json`、`readback-validation.json`、`replay-validation.json` 及请求/读取结果。只有发布和读回完成才标记发布成功；报告推理 completed 与 publication 状态分别记录。最终交付环境、report_id、发布时间、发布包路径及必要限制。

## 已验证案例

2026-09-08 `uat-trial-004`：v6 原件制作 v5 包后，UAT 返回 201；33 张总结、34 个链详情、99 个节点、0 个公司对象读回一致；105 个唯一 Evidence、337 个 scope、1102 次 Evidence 引用通过核对。此记录只证明该次包与当时接口，后续仍须核验环境、合同和来源，不能沿用历史计数。

概念聚合报告使用现有 concept_analyses 合同发布，不扁平化为 industry_chain_analyses；逐概念检查全部链及节点回读，按真实链 ID 另外记录去重计数。

无Concept映射的已评估链按用户要求保留 industry_chain_analyses，标题使用链正式名称、source_id保持ICH；不强制独立链数组为空，不伪造Concept归属。

2026-09-08 `uat-trial-005`：概念聚合与无关联链兜底包发布至 UAT，报告 `RPT984713b1-aaca-4e16-b421-be4dad8faa3d`。地缘5、宏观5、概念16、独立链7；33张总结、47个链详情（24个真实链）、89个节点上下文、0公司对象逐字段回读一致。96个唯一Evidence、370个scope及1047次引用核对通过。公司自身53条输入信号仅参与内部分析，不输出公司结果或公司信号表；HTML也不保留空公司章节。计数是本次验收记录，不作为后续固定要求。
