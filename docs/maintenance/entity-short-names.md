# Data 权威简称与图谱更新

四类 Data 实体的 `short_name` 映射到同名 Neo4j 属性：ChainNode、IndustryChain、MacroEconomic、GeopoliticRivalry。
允许 null，否则包含非空白字符且最多 5 个字符。正式 name、aliases、UUID、关系、向量和历史报告保持独立。

IndustryChain API DTO、ChainNode SQL 快照、宏观和地缘 joined snapshot DTO 均接收简称；旧快照缺字段兼容为 null。
宏观和地缘 export_snapshot.sql 已使用 to_jsonb(r)，自动携带新字段。普通投影会写入简称并读回验证。
旧快照的缺字段按 null 处理，因此已补简称后应使用最新 Data 快照，避免被历史快照清空。

## 属性更新包

Operator 在 Data PostgreSQL 中执行 `sematica/initialization/short_names/export.sql`：

```bash
psql -X -qAt -v ON_ERROR_STOP=1 "$DATA_EXPORT_DSN" \
  -f sematica/initialization/short_names/export.sql > source.json
```

只读单事务导出四表 id/name/short_name、来源数据库及时间；Data 数据库密钥不进入 AgentOS。
设置目标 NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD，按需 NEO4J_DATABASE（默认 neo4j）。不依赖 LLM 或 embedding。

```bash
python -m sematica.projection.short_names plan --input source.json --target dgx-uat --output plan.json
# 审阅 plan 中 source、before、short_name、missing；保存计划及校验和。
python -m sematica.projection.short_names apply --input plan.json --target dgx-uat
python -m sematica.projection.short_names verify --input plan.json --target dgx-uat
# 如需撤销：同一计划已包含原值；仍检查其他属性及更新后的简称未漂移。
python -m sematica.projection.short_names rollback --input plan.json --target dgx-uat
```

plan/verify 只读。计划包含冻结源数据及每个目标节点的原简称和其他属性 SHA-256；重复 ID、非权威 UUID、类型或正式名称不一致拒绝。
源中没有出现在图中的对象记录到 missing；默认拒绝 apply，确需仅处理存在节点时显式 --allow-missing。
数据包 source 中的对象覆盖四类，但不会处理无 data_object_id 的新闻候选节点。

apply/rollback 在一个 Neo4j 事务中锁定匹配节点，检查原值及其他属性指纹，再仅 SET short_name，最后读回验证。
null 删除该属性；任何检查失败回滚整批。不创建/删除节点，不操作关系，不调用模型或重新计算向量。
显式 --target 是操作员目标标识，仍须正确设置 Neo4j 连接；节点原值和指纹提供第二层环境/漂移校验。
图谱有持续业务写入时可能使计划失效，应重新 plan 和审阅，不绕过校验。
一次成功 apply 后用 verify 验证；重复 apply 在原值已改变时会拒绝。事务失败可在确认现状后重试。

代码经 PR 人工合并并部署后才执行 UAT apply。本次准备数据包不代表执行授权或已经更新 UAT。

## 验证

`python -m unittest discover -s tests -p test_short_names.py` 验证合同、投影、缺失/漂移及篡改拒绝。
设置 RUN_SHORT_NAMES_NEO4J_TEST=1 可运行本地 Neo4j 测试：在始终回滚的事务内创建随机测试实体，
验证更新、null 清除、回滚、向量和关系保留及漂移拒绝；结束确认测试实体没有提交。
