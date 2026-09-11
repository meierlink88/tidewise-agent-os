# 发布名称投影

推理、审计及分析 HTML 继续使用正式 name。制作独立 v5 发布包时，GPR（地缘）、MEC（宏观）、ICH（产业链）、CND（产业链节点）的显示名称字段使用目标环境图谱的 short_name 值。

字段名仍为原合同的 name；故事线和独立产业链单元使用 title 的地方仍为 title。不要新增 short_name 字段到发布合同。所有 source_id、local_key、图边端点、Event/Signal/Evidence ID、上游引用保持不变。ID 仅用于查询对应简称，不被替换或重写。

范围包括顶层单元 title、详情宏观 name、链 name、受影响节点 name 和 graph.nodes[].name。Concept 与公司不在本次简称范围。结论、推导正文、原始 Signal、Evidence 原文不做字符串替换；同名不同实体按 source_id 分别查找。同一实体所有显示位置使用同一冻结简称。

## 执行

1. 在已核验的目标 AgentOS 环境中只读导出简称目录，记录主机、容器、环境、查询时间和文件哈希。图谱字段为 short_name，不能把 aliases 当简称。命令需在包含此工具的代码版本执行：

   ```bash
   python -m capabilities.report_reasoning.tools.publication_names export-catalog --output /tmp/publication-name-catalog.json
   ```

2. 完成独立 v5 发布包其他兼容转换后，再投影名称：

   ```bash
   python -m capabilities.report_reasoning.tools.publication_names project \
     --report publication-v5/report.json \
     --catalog publication-name-catalog.json \
     --output publication-short-names
   ```

3. 目录按实体 ID 精确映射；引用实体无简称、空白简称或目录重复 ID 时停止生成，报告具体 ID，不自行缩写或静默退回全名。旧冻结分析输入可能没有简称字段，应另冻目标目录，不改旧分析输入。
4. 保存原件、目录和 name-projection-receipt.json 的路径、哈希与逐字段前后值。名称投影只有 name/title 值变化，不增加/删除分析内容。推理校验仍对正式名称原件执行；简称发布包按 Data 合同校验，不为其改写分析目录的正式名称。
5. 以投影结果冻结最终 request.json，重新核对 Evidence、执行 Data 业务校验后发布；读回每个名称字段并对照简称包，同时确认 ID、引用、结论与证据未改变。旧包成功或结果不确定时不能更换同一发布身份下的内容；遵守既有冲突与重放规则。

此机制不自动更新图谱，不自动覆盖或重发已经发布的历史报告。
