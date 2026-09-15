# 发布显示名称

推理使用正式名称；发布时保持原有四类实体（GPR/MEC/ICH/CND）的short_name投影规则，Concept不擅自改名。身份及引用不变，没有真实简称则阻断名称投影，不自行造简称。

当前export-catalog仍可使用`python -m capabilities.report_reasoning.tools.publication_names export-catalog --output /absolute/catalog.json`，在核验过的数据环境只读执行并冻结查询时点及哈希。旧CLI的project只接受v5，不用于新流程。

正式v6由`../run-investment-report/scripts/project_v6.py --name-catalog /absolute/catalog.json`完成名称投影与结构投影，保存逐字段before/after。复核原始信号文本、结论、图关系和source_id均未随名称变化。不要把地缘无等同资产可保留原名的政策替换为本Skill的四类实体目录规则。
