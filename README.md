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

本服务通过 `tidewise-local` 网络复用 `tidewise-infra` 中的 PostgreSQL，使用独立数据库
`agent_os`。Compose 项目名固定为 `tidewise-app`，本仓库只启动容器 `agent-os`。

```text
tidewise-app
└── agent-os         # 仅在本仓库显式启动

tidewise-infra
└── postgres-1       # 共享实例，AgentOS 使用独立 agent_os 数据库
```

## 启动

```bash
cp example.env .env
# 填写 DEEPSEEK_API_KEY、DB_PASS 和 MinIO 凭据；首次初始化还需创建 agent_os 数据库/角色。

docker compose up -d --build agentos
curl -sSf http://localhost:8000/health
# 仅在新的 agent_os 数据库中执行一次：
docker compose exec -T agentos python -m scripts.seed_schedules
./scripts/mcp_check.sh
```

只操作本服务：

```bash
docker compose logs -f agentos
docker compose restart agentos
docker compose stop agentos
docker compose rm -f agentos
```

> 不要在本仓库执行不带服务名的 `docker compose down` 或 `--remove-orphans`：
> `tidewise-app` 分组还包含其他观潮家应用服务。

## 采集提示词与数据

`raw-collector` 首次启动时由 `agents/raw_collector.seed.md` 创建 Studio 发布版本。之后在 AgentOS
Control Plane 的 Studio 中编辑并发布 Instructions；`raw-collection` 每次运行从 PostgreSQL
加载当前发布版，无需重启容器。Agent 只把提示词的语义目标规划为 `query`
与 `lookback_hours`；Workflow 冻结本次通道快照，并使用同一截止时间确定性并行执行三类采集能力。

`raw-collection` 首次启动时也会创建一个 Studio 发布版本。四步 Workflow 编排可在 Studio
中创建新版本并发布；步骤使用的 Agent 工具和自定义 Function 实现在 Git 中维护。共享采集
采集实现集中在 `capabilities/collection/`，Evidence 实现集中在 `capabilities/evidence/`；每个领域只以 `tools/`、`functions/`、`internal/` 组织，不属于某个 Agent 或 Workflow 私有。
采集 Workflow Executor 使用 Agno 异步运行接口，所有外部通道使用异步 HTTP；Tool Batch、
Artifact 构建和发布的文件操作会卸载到工作线程，因此单 Worker 运行采集时不会阻塞其他业务
Agent 或 Workflow。

新环境显式执行一次 `python -m scripts.seed_schedules` 后，会得到默认的
`raw-collection-hourly` 和 `evidence-extraction-every-10-minutes` Schedule。默认名称只用于首次
创建；之后名称、cron、endpoint、payload 和启停状态均由 PostgreSQL 与 AgentOS Control Panel
管理。应用启动仅按 Workflow endpoint 做只读缺失、重复和启停检查，不创建或覆盖 Schedule，
因此 Control Panel 中的改名和其他运行配置会跨容器重启保留。

采集结果默认位于项目根目录 `data/collector/`，其中接受的文章在 manifest 可见前还会以同一
`documents/YYYY/MM/DD/<document-sha256>.md` 内容寻址对象键幂等发布到 MinIO `raw-evidence` bucket：

- `documents/`：接受的原始资讯 Markdown。
- `runs/<run_id>/`：候选账本、汇总和 manifest。
- `indexes/title-dedup-index.tsv`：新版标题跨运行去重索引；历史 `dedup-index.tsv` 仅保留为只读 URL 兼容索引。

`data/` 已被 Git 忽略。可通过 `.env` 的 `COLLECTOR_DATA_DIR` 替换宿主机目录。MinIO bucket 需要预先创建并允许
浏览器下载；`MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY` 只用于对象发布。Data Service
的 `raw_text` 保存 `/{bucket}/{object_key}`，例如 `/raw-evidence/documents/2026/08/15/<sha256>.md`，不保存环境
Base URL。浏览器使用 MinIO 对外 Base URL 与该路径直接拼接。

采集 Agent 不负责调用通道，只输出严格查询计划。Workflow 的确定性 Function 每次各执行一次
`web_fetch`、`api_fetch`、`rss_fetch` 共享实现。三个 Tool 门面仍在 Registry 中可见，用于独立验证。通道实例保存在 AgentOS
PostgreSQL 的 `collection_channels` 表：Web Search 最多启用一个，API 与 RSS 会有界并发执行全部
启用通道。固定通道不可删除但可以禁用；动态 RSS/Atom 通道使用 `generic_rss` Adapter，可直接
新增和删除。`priority=1` 表示最高优先级。

首次启动会从 `.env` 幂等插入 7 个固定通道，并把搜索 Key 和 Base URL 写入缺失的新行；已存在
行绝不会被启动过程覆盖。此后直接修改表内的 `enabled`、`endpoint`、`app_key`、`config`、
`priority`、`timeout_seconds`、`max_results` 或 `default_source_level`，下一次 Workflow 运行即生效，
无需重启容器。当前阶段 `app_key` 按明确决策明文存储；Tool 不会把它返回给模型或 Artifact。

Tavily 固定请求 `include_raw_content: "text"`，避免使用供应商的 HTML→Markdown 转换；AgentOS 把各通道
已归一的文本统一包装为带 YAML frontmatter 的 Markdown Artifact。

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

## UAT

AgentOS 通过独立 GitHub Action 发布到与 Tidewise AI 共用的华为云 ECS。发布复用 SWR、RDS
Instance 和外部 `tidewise-uat` Docker 网络，但使用独立的 `agent_os_uat` 数据库、Compose
项目、持久化目录和回滚状态。UAT 应用端口在 Compose 中固定为仅回环可见的 `9081`，
公网通过 `https://tideai.tripwise.cn/agentos` 访问；部署只允许从 `main` 手工触发，并使用镜像 digest。完整操作见
[infra/uat/README.md](infra/uat/README.md)。
