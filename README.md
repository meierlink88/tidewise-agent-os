# Tidewise AgentOS

观潮家自托管 AgentOS 运行时，基于 Agno。Agent、Team、Workflow 都是本仓库的核心交付物；`app/main.py` 只负责装配，不承载业务实现。

## 当前组件

- Agent：`tidewise-assistant`，默认使用 DeepSeek V4 Flash。
- Agent：`raw-collector`，由 Agno Studio/PostgreSQL 管理提示词版本，由 Workflow 调用。
- Workflow：`local-ping`，无模型依赖的运行时检查。
- Workflow：`deployment-check`，检查数据库、MCP、组件和调度状态。
- Workflow：`raw-collection`，由 Agno Studio/PostgreSQL 管理编排版本，执行 Agent 采集、确定性去重和 manifest-last 发布。
- API/MCP：`http://localhost:8000`、`http://localhost:8000/mcp`。

## 本地拓扑

本服务复用已有 `local-postgres-1:5432`，使用独立数据库 `agent_os`。Compose 项目名固定为 `local`，所以 Docker Desktop 中显示在 `local` 分组下，服务名为 `agentos-1`。

```text
local
├── agentos-1        # 本仓库
├── agentrun-1
├── data-1
├── miniapp-1
├── adminportal-1
└── postgres-1       # 共享实例，AgentOS 使用独立 agent_os 数据库
```

## 启动

```bash
cp example.env .env
# 填写 DEEPSEEK_API_KEY 和 DB_PASS；首次初始化还需创建 agent_os 数据库/角色。

docker compose up -d --build agentos
curl -sSf http://localhost:8000/health
./scripts/mcp_check.sh
```

只操作本服务：

```bash
docker compose logs -f agentos
docker compose restart agentos
docker compose stop agentos
docker compose rm -f agentos
```

> 不要在本仓库执行不带服务名的 `docker compose down`：`local` 分组还包含其他观潮家本地服务。

## 采集提示词与数据

`raw-collector` 首次启动时由 `agents/raw_collector.seed.md` 创建 Studio 发布版本。之后在 AgentOS
Control Plane 的 Studio 中编辑并发布 Instructions；`raw-collection` 每次运行从 PostgreSQL
加载当前发布版，无需重启容器。提示词中的时间约束由 Agent 换算为每个工具调用的
`published_after` / `published_before`，工具和 Artifact 层会再做确定性验证。

`raw-collection` 首次启动时也会创建一个 Studio 发布版本。三步 Workflow 编排可在 Studio
中创建新版本并发布；步骤使用的 Agent 工具和自定义 Function 实现在 Git 中维护。共享采集
实现集中在 `capabilities/raw_collection/`，不属于 Agent 或 Workflow 的私有代码。
采集 Workflow Executor 使用 Agno 异步运行接口，所有外部通道使用异步 HTTP；Tool Batch、
Artifact 构建和发布的文件操作会卸载到工作线程，因此单 Worker 运行采集时不会阻塞其他业务
Agent 或 Workflow。

本地运行时会注册 `raw-collection-hourly` Schedule：按 `Asia/Shanghai` 时区每个整点
执行一次 `raw-collection`，采集最近 48 小时的信息并发布 Artifact。Schedule 首次创建时
默认启用；之后可在 AgentOS Control Panel 的 Scheduler 页面暂停、恢复、手工触发和查看运行历史，
启用状态不会被容器重启覆盖。

采集结果默认位于项目根目录 `data/collector/`：

- `documents/`：接受的原始资讯 Markdown。
- `runs/<run_id>/`：候选账本、汇总和 manifest。
- `indexes/dedup-index.tsv`：跨运行去重索引。

`data/` 已被 Git 忽略。可通过 `.env` 的 `COLLECTOR_DATA_DIR` 替换宿主机目录。

采集 Agent 每次运行会逐一调用 Parallel Search、Tavily、博查、财联社电报、东方财富 7x24、
东方财富个股新闻和证券时报快讯；未指定时间时默认采集最近 48 小时。前三个搜索通道需要在 `.env` 分别配置 `PARALLEL_API_KEY`、
`TAVILY_API_KEY`、`BOCHA_API_KEY`；留空时该通道安全返回未配置，其他公开通道继续可用。
Key 或 Base URL 修改后执行以下命令让容器重新读取环境变量：

```bash
docker compose -p local up -d --force-recreate agentos
```

## 开发

```bash
./scripts/venv_setup.sh
source .venv/bin/activate
./scripts/format.sh
./scripts/validate.sh
```

新增组件时：

- Agent 放在 `agents/<slug>.py`。
- Workflow 放在 `workflows/<slug>.py`；确定性工序优先使用 Workflow。
- Team 只在确有多个自治角色协同时创建，放在 `teams/<slug>.py`。
- 在 `app/main.py` 注册，并在 `app/config.yaml` 添加展示信息。
- 先验证 `local-ping`，再验证模型 Agent，最后验证 MCP。

详细初始化决策见 [docs/design/tidewise-agentos-initialization.md](docs/design/tidewise-agentos-initialization.md)。
