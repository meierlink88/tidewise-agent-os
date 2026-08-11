# Tidewise AgentOS 本地初始化设计

## 目标

把官方 AgentOS Docker 模板收敛为观潮家独立维护的运行时项目：

- AgentOS API 由 Docker 启动，并归入 Docker Compose 项目 `local`。
- 复用 `local-postgres-1` 的 PostgreSQL 5432，不创建第二个 PostgreSQL 容器。
- 使用独立数据库 `agent_os` 和最小权限登录角色 `agent_os_runtime`。
- 默认模型为 DeepSeek V4 Flash，密钥只保存在被 Git 忽略的 `.env`。
- 首批交付只包含一个代码定义 Agent、一个无模型健康 Workflow 和一个部署检查 Workflow。

## 运行时边界

| 组件 | 责任 | 真源 |
|---|---|---|
| `agents/tidewise_assistant.py` | 通用对话入口和运行链路验收 | Git |
| `workflows/local_ping.py` | 无 LLM、可重复的本地健康检查 | Git |
| `workflows/deployment_check.py` | DB、MCP、组件、调度配置检查 | Git |
| `app/main.py` | 注册组件、接口和生命周期 | Git |
| Agno/PostgreSQL 表 | 会话、运行、调度与追踪状态 | `agent_os` 数据库 |
| `.env` | 本机密钥与连接参数 | 本机，不进 Git |

生产 Agent、Team、Workflow 后续均按业务域拆文件，不堆入 `app.py`。Studio 中临时创建的组件不能替代 Git 中的生产定义；验证后需要回写源码和测试。

## 数据与安全

- 宿主机仍只暴露已有 PostgreSQL 5432；AgentOS 通过外部网络 `tidewise-local` 和别名 `postgres` 访问它。
- `agent_os_runtime` 仅拥有 `agent_os`，不授予其他业务数据库权限。
- 本地使用 `RUNTIME_ENV=dev`，JWT 关闭；生产必须切换到 `prd` 并配置 JWT/JWKS。
- DeepSeek API Key 不写入镜像、Compose 文件、日志或 Git。

## 启动与失败策略

- 启动命令限定为 `docker compose up -d --build agentos`。
- 因为本服务与其他本地服务共用 Compose 项目名 `local`，禁止在本仓库执行不带服务名的 `docker compose down`。
- DB 不可用时入口等待最多 300 秒；注册调度失败只告警，不阻断 API。
- `local-ping` 不依赖模型，先用于区分平台故障与模型供应商故障。

## 验收

1. `docker compose config` 只包含 `agentos` 服务，网络指向外部 `tidewise-local`。
2. Docker 标签为 `com.docker.compose.project=local`、`service=agentos`。
3. `agent_os` 数据库存在且归属 `agent_os_runtime`。
4. `/health`、`/agents`、`/workflows` 返回成功。
5. `local-ping` Workflow 成功且不产生模型调用。
6. `tidewise-assistant` 使用 DeepSeek V4 Flash 返回非空内容。
7. `/mcp` 握手、工具枚举和 `run_agent` 成功。
8. 格式、静态检查和数据库持久化检查通过。

## 非目标

- 本轮不实现数据采集业务流水线；采集将作为独立确定性 Workflow 设计。
- 本轮不创建 Team；只有在存在真实的多角色协作和独立上下文需求时引入。
- 本轮不部署公网、不连接 Agno Control Plane，也不启用生产鉴权。
