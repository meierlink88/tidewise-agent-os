# Tidewise AgentOS

观潮家自托管 AgentOS 运行时，基于 Agno。Agent、Team、Workflow 都是本仓库的核心交付物；`app/main.py` 只负责装配，不承载业务实现。

## 当前组件

- Agent：`tidewise-assistant`，默认使用 DeepSeek V4 Flash。
- Agent：`title-curator`（Raw Evidence Filter），对采集素材做投研相关性判断，由 Raw Collection Workflow 调用。
- Agent：`evidence-extractor`，从 Raw Evidence 提取 Atomic Evidence，由 Studio/PostgreSQL 管理。
- Agent：`event-extractor`，按主体、动作、对象、阶段与时间身份语义把同批 Evidence 提炼为 Event Candidate。
- Agent：`investment-reasoner`、`investment-report-writer`、`investment-reviewer`，分别负责分层影响与 Signal 传导、报告中文撰写和推理/报告审核。
- Workflow：`local-ping`，无模型依赖的运行时检查。
- Workflow：`deployment-check`，检查数据库、MCP、组件和调度状态。
- Workflow：`raw-collection`，由 Agno Studio/PostgreSQL 管理编排版本，执行采集、语义过滤、确定性去重和 manifest-last 发布。
- Workflow：`evidence-extraction`，增量提取并发布 Atomic Evidence，回写正式 Evidence ID。
- Workflow：`event-extraction`，冻结本地 Evidence，发布去重后 Event，投影 Graphiti 并构建 Signal Fact。
- Workflow：`investment-reasoning`，由 Schedule 命题直接触发，按地缘政治→宏观经济→产业链及节点逐层推导，仅从有效 Signal 根形成方向结论；生成固定报告后由独立幂等发布 Step 交付，当前本地默认使用文件 Mock Publisher。
- Projection CLI：`sematica.projection.company_cli`，从 Data API 投影 canonical Company，并只对图中已有 Industry/ChainNode 做可恢复的受限模型映射；写入禁止使用 Graphiti Episode。
- API/MCP：`http://localhost:8000`、`http://localhost:8000/mcp`。

## 本地拓扑

本仓库拥有独立的 `agent-os` Compose 项目，同时管理 AgentOS 与 Graphiti 使用的
Neo4j。AgentOS 仍通过 `tidewise-local` 外部网络复用 `tidewise-infra` 中的
PostgreSQL，使用独立数据库 `agent_os`；Data Service 也通过该共享网络访问。

```text
agent-os
├── agent-os-service  # Agno AgentOS
└── agent-os-neo4j    # Graphiti 图存储，复用历史数据卷

tidewise-infra
└── postgres-1       # 共享实例，AgentOS 使用独立 agent_os 数据库
```

## 启动

```bash
cp example.env .env
# 填写 DeepSeek、Neo4j、Graphiti、DB、Data Service 和 MinIO 凭据；
# 首次初始化还需创建 agent_os 数据库/角色。

docker compose up -d --build agentos neo4j
curl -sSf http://localhost:8000/health
# 仅在新的 agent_os 数据库中执行一次：
docker compose exec -T agentos python -m scripts.seed_schedules
./scripts/mcp_check.sh
```

只操作本服务：

```bash
docker compose logs -f agentos
docker compose restart agentos
docker compose stop agentos neo4j
docker compose rm -f agentos neo4j
```

> Neo4j 数据卷明确复用 `tidewise-reason_graphiti-neo4j-data`。不要执行
> `docker compose down -v` 或手工删除该卷，否则会丢失图谱数据。

从历史 `tidewise-reason` 本地配置首次迁移时，可执行：

```bash
./scripts/migrate_graphiti_runtime_env.sh
```

该脚本只合并 AgentOS `.env` 中缺失的 Neo4j/Graphiti 键，不覆盖已有值，也不输出密钥。

## 采集提示词与数据

`raw-collection` 直接使用 Schedule message 作为采集 query，不再注册或运行 Query Planner Agent。
Workflow 仅包含 `collect-raw-evidence`、`filter-raw-evidence` 和 `publish-raw-evidence` 三个业务步骤。
`Raw Evidence Filter` 由 `agents/title_curator.py` 维护，结合标题、来源、发布时间和有界正文摘要排除非政经素材。

`raw-collection` 首次启动时也会创建一个 Studio 发布版本。Workflow 编排可在 Studio
中创建新版本并发布；步骤使用的 Agent 和自定义 Function 实现在 Git 中维护。采集
实现集中在 `capabilities/collection/`，Evidence 实现集中在 `capabilities/evidence/`，Event Candidate
提炼与交接集中在 `capabilities/event/`，Company 受限推断与冻结决策集中在 `capabilities/company/`，
投研推理规则集中在 `capabilities/investment/`；每个领域只以 `tools/`、`functions/`、`internal/` 组织，
不属于某个 Agent 或 Workflow 私有。Graphiti 的时间检索与图驱动适配仍保留在 `sematica/graphiti/`。
采集 Workflow Executor 使用 Agno 异步运行接口，所有外部通道使用异步 HTTP；Tool Batch、
Artifact 构建和发布的文件操作会卸载到工作线程，因此单 Worker 运行采集时不会阻塞其他业务
Agent 或 Workflow。

新环境显式执行一次 `python -m scripts.seed_schedules` 后，会得到默认的
`raw-collection-hourly`、`evidence-extraction-every-10-minutes`、
`event-extraction-every-minute` 和 `investment-reasoning-daily` Schedule。默认名称只用于首次
创建；之后名称、cron、endpoint、payload 和启停状态均由 PostgreSQL 与 AgentOS Control Panel
管理。应用启动仅按 Workflow endpoint 做只读缺失、重复和启停检查，不创建或覆盖 Schedule，
因此 Control Panel 中的改名和其他运行配置会跨容器重启保留。

采集结果默认位于项目根目录 `data/collector/`。接受的文章在 manifest 可见前以
`documents/YYYY/MM/DD/<document-sha256>.md` 内容寻址路径同时幂等写入 AgentOS 自有 MinIO
`raw-evidence` bucket 和本地 Artifact：

- `documents/`：接受的原始资讯 Markdown。
- `runs/<run_id>/`：候选账本、汇总和 manifest。
- `indexes/title-dedup-index.tsv`：新版标题跨运行去重索引；历史 `dedup-index.tsv` 仅保留为只读 URL 兼容索引。

`data/` 已被 Git 忽略。可通过 `.env` 的 `COLLECTOR_DATA_DIR` 替换宿主机目录。
Evidence Extraction 仍用本地完整 Markdown 作为模型输入，但通过版本化 Data Service API 的既有
`raw_text` 字段只提交 `/{bucket}/{object_key}` 相对路径。Data Service 不读取或代理对象，也不向 AgentOS
暴露自己的 PostgreSQL、MinIO 或其他基础设施；AgentOS 只持有自身 MinIO 的写凭据。

确定性 Function 使用 `DATA_SERVICE_TOKEN` 从 Data Service 读取一次完整 active Source Snapshot，
并行执行 Web Search、API 和 RSS 三类采集组。这三类能力不向 Agent Registry 注册为 Tool。
Web Search 最多启用一个，API 与 RSS 会有界并发执行全部启用 Source；动态 RSS/Atom Source 使用
`generic_rss` Adapter。`priority=1` 表示最高优先级。各通道取配置的最新 `max_results`，博查固定
`freshness=oneDay`；采集发布不再按本地时间窗口淘汰 Candidate。

Source 的创建、启停、Endpoint、Provider Key、配置与持久化由 Data Service 独占管理；AgentOS 不再创建、
Seed、CRUD 或读取本地 `collection_channels`。Snapshot 获取或完整性校验失败时，Workflow 在采集阶段
fail closed，不使用部分数据、缓存或旧表兜底。当前服务信任边界内 `app_key` 由 Data Snapshot 明文提供，
但不会返回给模型、日志或 Artifact。

Tavily 固定请求 `include_raw_content: "text"`，避免使用供应商的 HTML→Markdown 转换；AgentOS 把各通道
已归一的文本统一包装为带 YAML frontmatter 的 Markdown Artifact。

## 开发

```bash
./scripts/venv_setup.sh
source .venv/bin/activate
./scripts/format.sh
./scripts/validate.sh
```

Agno 主版本升级要求在新运行时提供流量前执行数据库迁移。本地先构建候选镜像，再停止
AgentOS、运行幂等迁移并重新启动：

```bash
docker compose build agentos
docker compose stop agentos
docker compose run --rm --no-deps agentos python -m scripts.migrate_agno_db
docker compose up -d agentos neo4j
```

迁移不会删除 Agno v2 的 legacy runs 数据；确认新版运行稳定前不执行 cleanup。

新增组件时：

- Agent 放在 `agents/<slug>.py`。
- Workflow 放在 `workflows/<slug>.py`；确定性工序优先使用 Workflow。
- Team 只在确有多个自治角色协同时创建，放在 `teams/<slug>.py`。
- 在 `app/main.py` 注册，并在 `app/config.yaml` 添加展示信息。
- 先验证 `local-ping`，再验证模型 Agent，最后验证 MCP。

详细初始化决策见 [docs/design/tidewise-agentos-initialization.md](docs/design/tidewise-agentos-initialization.md)。
Company 投影的关系门禁、无 Episode 写入边界和可恢复操作见
[docs/design/company-graph-projection.md](docs/design/company-graph-projection.md)。

## UAT

AgentOS 通过独立 GitHub Action 发布到 DGX Spark。DGX 自托管 Runner 从已验证的 `main` 精确提交
直接构建 ARM64 镜像，再由本机 Docker Compose 启动 AgentOS、独立 PostgreSQL 和 AgentOS 专属 Neo4j；
发布不依赖华为云 SWR 或旧 ECS。UAT 应用端口在 Compose 中固定为仅回环可见的 `9081`，公网通过
`https://tideai.tripwise.cn/agentos` 访问；部署只允许从 `main` 手工触发，并使用本地 image ID 固定构建产物。完整操作见
[infra/uat/README.md](infra/uat/README.md)。
