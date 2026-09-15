# 脚本接口与运行文件

运行时使用 AgentOS 现有虚拟环境的 Python 3.12 或更新版本（如仓库根 `.venv/bin/python`），脚本自身只依赖标准库。下文 python 指该解释器，不使用 macOS 自带旧版 Python。

以下命令由Codex逐步执行，不是一个自动运行模型的shell脚本。`RUN`为新的绝对输出目录，路径作为独立参数传递。先准备数据再init；本Skill不会猜测环境、日期或认证。所有JSON须UTF-8，拒绝重复key和非有限数字。

## scope与输入

`scope.json`必填：environment、start、end、timezone、market、research_base_url、data_base_url、source_identity、method_files。时间使用含时区ISO8601，start<end且end不晚于当前时间。source_identity记录实际hostname、容器、代码/镜像、Event数据路径、查询时点等已核实信息，不能只写uat字符串。method_files列出本批实际使用的两个分析Skill、工作流及配套方法/Schema/转换器的绝对文件路径；脚本记录SHA，恢复时发现改变会阻断。绝不保存凭据或完整环境变量。

`--snapshot`采用现有prepare产生的snapshot.json：events、signals、entities、structure、evidences、window、selection_time_field、source_kind。只接受created_at口径和与scope一致的window。原CLI查询含两端，本脚本排除end时刻Event；关联Signal若跨出本批范围则阻断，不能剪断来源。输出input/manifest.json兼容原CLI，两个分析上下文的query和validate都指向这个新input/。

```sh
python scripts/workflow.py --run /absolute/RUN init --scope /absolute/scope.json --snapshot /absolute/cli-input/snapshot.json
python scripts/workflow.py --run /absolute/RUN status
```

取数由现有只读CLI完成，Script不假称有REST取数适配。零Event真实快照可冻结；旧prepare若拒绝空输入，保留查询证据，不用虚构Event凑数。

## 1. 故事线选择

Codex保存selection.json：

```json
{
  "snapshot_sha256": "本批state.snapshot.sha256",
  "event_reviews": [{"event_id":"真实EVT", "story_ids":["真实GPR"], "reason":"关联或无关联的实际理由"}],
  "stories": [{"story_id":"真实GPR", "name":"目录正式名称", "event_ids":["真实EVT"], "decision":"research", "reason":"本期变化值得研究的理由"}]
}
```

每个新Event恰好有一条review。相关故事线允许多选；每个候选故事线恰好一条research或skip决策。真实目录只验证身份，相关性由Codex核对Event/工具候选。无候选时stories=[]，Event review仍需覆盖。

```sh
python scripts/workflow.py --run /absolute/RUN select --selection /absolute/selection.json
```

## 2. Research

```sh
python scripts/workflow.py --run /absolute/RUN research-submit --story-id GPR...
python scripts/workflow.py --run /absolute/RUN research-poll --story-id GPR...
```

认证仅从TIDEWISE_RESEARCH_API_KEY读取；明确部署允许无鉴权时可为空。URL可为DGX内网，无须开放公网。每命令一个HTTP请求、30秒超时、不跟随重定向；poll由Codex按进度间隔调用。submitting/unknown没有已存run_id时停止，需根据服务端确切创建记录确认；不自动选latest、不重POST。确认后用`research-bind --story-id GPR... --run-id RUN... --proof /absolute/proof.json`绑定，proof记录run_id、story_id、原POST正文request_sha256和服务创建记录source_reference；脚本GET核对preset与完整user_vars后只恢复查询。不能仅按同名或latest创建proof。已知run_id的running可继续GET。failed/cancelled不自动重试为另一run。

脚本下载API的final_report UTF-8原样保存，绑定story/run/window及SHA。它保证所返回正文的精确保存，不冒称与服务端文件末尾换行逐字节一致，也不保证Research查询数据的事务快照一致性。

## 3–5. 包与审阅

每个lane的report.json是正式v6 Report（尚不包含publisher_report_id），四类集合均提供；只填本lane归属数组，company_analyses=[]。统一的report_type、generated_at、timezone、analysis_window在各分析上下文开始前由主Codex提供，只含批次身份与时间，不携带其他层结论。各路observations、limitations独立保存，assemble按地缘→宏观→产业顺序原样拼接，仅去除完全相同项；不跨层归纳或重写，避免为统一头部丢掉业务限制。时间不能冒充原研究日期。不能把运行诊断塞进产品字段。

宏观和产业在原件审阅后各执行一次：

```sh
python scripts/project_v6.py --source /absolute/macro-draft.json --converter /absolute/tidewise-ai/data-service/backend/scripts/report-unify.py --converter-sha256 FROZEN_SHA --name-catalog /absolute/catalog.json --output /absolute/macro-v6.json
```

该命令不是merge；它在分路包生成之前进行结构投影。具体content.json→地缘正式v6的方向、范围和指标语义由compose Skill提炼，不由脚本臆测。

每份`review.json`为`{"status":"passed","artifact_sha256":"report.json字节SHA256","reviewer":"实际审阅上下文标识","findings":[]}`。先完成真实语义审阅再填写，不模板式盖章。

```sh
python scripts/workflow.py --run /absolute/RUN pack --lane geopolitics --report /absolute/geo-v6.json --review /absolute/geo-review.json
python scripts/workflow.py --run /absolute/RUN pack --lane macroeconomics --report /absolute/macro-v6.json --review /absolute/macro-review.json
python scripts/workflow.py --run /absolute/RUN pack --lane industry --report /absolute/industry-v6.json --review /absolute/industry-review.json
```

pack冻结已有内容，不代表Data校验通过。发现新缺陷时保留旧批次和版本；新版本重新准备相关包和凭据，不覆盖旧hash。已经完成研究可经明确相同来源绑定复用到新工作任务，但当前CLI不提供自动跨batch导入或自动重推功能，不能手改状态冒充执行。

## 6. 装配与发布

```sh
python scripts/workflow.py --run /absolute/RUN assemble
python scripts/workflow.py --run /absolute/RUN validate --data-repo /absolute/tidewise-ai
```

assemble只按lane提取对应集合，其他业务字段原样不动。metadata不等、缺lane、研究未完成、错版本、空全报告或引用错误都会失败。输出candidate.json与固定publisher_report_id。validate调用官方Go业务校验器，无数据库写入，失败凭据保留。Go源码版本不证明目标已部署。

Codex按两个Skill的发布规则完成目标身份/部署合同核验与Evidence预检，写preflight：request_sha256、environment、data_base_url、status=passed、deployed_contract_verified=true、evidence_verified=true，并保存实际查询证据。这些不是由脚本凭空验证的布尔值。

仅用户实际调用工作流并授权目标发布时：

```sh
python scripts/workflow.py --run /absolute/RUN publish --preflight /absolute/preflight.json
python scripts/readback.py --run /absolute/RUN --max-requests 20
```

认证从DATA_SERVICE_BEARER_TOKEN读取。publish只接受当前哈希的通过校验与目标预检；最多首次+一次同包重放。4xx为rejected不盲重试；5xx/连接不明为unknown。第二次不确定须外部核实，不换键发布。当前服务回执无content_hash；本地request_sha256、Go校验输出的归一化content_hash、服务report_id/published_at分开保存，不能虚构服务哈希。

readback每次最多指定次数请求，缓存已读响应并校验哈希。重复运行直至capture_complete=true，完成各集合分页、确切单元详情和Evidence scopes，检查单元顺序/数量与scope数量。接着Codex按Data读出投影逐字段比较所有结论、推导、指标、资产、图、Signal及Evidence内容；枚举/引用投影不能直接用原始JSON字节比较。不得以抓取完成代替内容一致。

用原publish命令同包重放一次，核对replayed=true、相同report_id与published_at。填写readback-review：status=passed、request_sha256、report_id、all_pages_verified、all_fields_verified、evidence_scopes_verified、same_package_replay_verified均为true，artifacts列出运行目录内实际读回产物的path和sha256。

```sh
python scripts/workflow.py --run /absolute/RUN complete --review /absolute/readback-review.json
```

状态字段分别保存selection、research、lanes、candidate、validation、publication、readback。HTTP成功只得到published_unverified，完整审阅和重放完成才completed。恢复使用原run路径；查status后从未完成阶段继续。脚本不会创建任务、代理、Agno组件或定时器。
