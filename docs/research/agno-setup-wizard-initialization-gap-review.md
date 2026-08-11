# Agno Setup Wizard 初始化差距审查

日期：2026-08-10

## 结论

`agno-setup-wizard` 不是脚手架、CLI 或代码生成器，而是一份可复制的引导 Prompt。它的主要作用是让编码 Agent 根据部署目标克隆官方 AgentOS 模板，然后把实际初始化交给模板自带的 README 和 `.agents/skills/`。官方 README 对这一点有明确说明：[Setup Wizard README](https://github.com/agno-agi/agno-setup-wizard/blob/cc2440afc3253ef76bc1aa84f69348b073e1c0e3/README.md#L1-L22)。

因此，当前项目没有遗漏“安装 setup wizard”。已经从 `agentos-docker` 建立新仓库，等于完成了 wizard 的目标选择与模板克隆。真正未完成的是两层工作：

1. 模板的本地 setup 尚未执行；
2. 直接 setup 前尚未完成 Tidewise 项目级定制。

建议先完成 Tidewise 基线定制，再运行 `/setup-platform`。

## Wizard 实际要求的流程

官方 canonical prompt 定义的通用流程为：

1. 选择部署目标并克隆对应模板；
2. 读取模板 README 与 `.agents/skills/`；
3. 检查 Docker，创建 `.env`，启动 Compose；
4. 验证 `http://localhost:8000/docs`；
5. 连接 `os.agno.com` Local connection；
6. 创建第一个 Agent；
7. 使用 Platform Manager 检查平台健康度。

来源：[canonical wizard flow](https://github.com/agno-agi/agno-setup-wizard/blob/cc2440afc3253ef76bc1aa84f69348b073e1c0e3/agno-setup-wizard-prompt.md#L25-L36)。Wizard 支持 Docker、Railway、AWS、GCP、Azure、Fly.io、Render、Modal 和 Kubernetes 目标；本项目已经选择 Docker：[setup targets](https://github.com/agno-agi/agno-setup-wizard/blob/cc2440afc3253ef76bc1aa84f69348b073e1c0e3/agno-setup-wizard-prompt.md#L44-L61)。

## 当前状态

| 项目 | 状态 | 证据/备注 |
| --- | --- | --- |
| 选择 Docker 模板 | 已完成 | 新仓库来自 `agentos-docker` |
| 读取 README/skills | 已完成 | 当前 `AGENTS.md` 和 setup skill 可用 |
| Docker daemon | 已就绪 | `docker info` 成功，Server 29.2.1 |
| `.env` | 未创建 | 工作区中无 `.env` |
| AgentOS Compose | 未启动 | `docker compose ps` 无本项目容器 |
| `/docs` 验证 | 未完成 | API 尚未启动 |
| MCP smoke | 未完成 | 尚未运行 `scripts/mcp_check.sh` |
| AgentOS UI Local connection | 未完成 | 需要用户登录后连接 |
| 首个 Tidewise 组件 | 未完成 | 不建议现在创建官方 Radar 示例 |
| Platform health | 未完成 | 需在启动后执行 |

当前仓库的 `setup-platform` skill 也要求 Docker → `.env` → Compose → MCP → UI → first agent：[setup-platform skill](../../.agents/skills/setup-platform/SKILL.md)。

## 立即阻塞项

### 1. PostgreSQL 宿主机端口冲突

当前主机 `5432` 已被 `local-postgres-1` 占用，模板 `compose.yaml` 又固定发布 `5432:5432`。直接执行 `docker compose up` 会在数据库容器启动时失败。

应在 setup 前：

- 使用项目专属 Postgres，不复用其他项目数据库；
- 容器内仍使用 `5432`；
- 宿主机发布端口改为可配置的 `127.0.0.1:5433` 或其他空闲端口；
- 维持 API 容器通过 Compose network 访问 `agentos-db:5432`。

### 2. 模型与凭据路径不符合 Tidewise 目标

Wizard 强调 Agno 是 model-agnostic：[model-agnostic guidance](https://github.com/agno-agi/agno-setup-wizard/blob/cc2440afc3253ef76bc1aa84f69348b073e1c0e3/agno-setup-wizard-prompt.md#L11-L23)。但当前模板实际使用 `OpenAIResponses` 和 `OPENAI_API_KEY`，而 Tidewise 已经决定使用 DeepSeek。

在复制 `example.env` 前应先明确：

- DeepSeek 的固定 model id、base URL 和环境变量名；
- 是否保留 Chief/Knowledge 的 OpenAI embedding 依赖；
- 若不保留，删除相关示例和 `OPENAI_API_KEY` 必需性；
- 凭据只写入已忽略的 `.env`，不进入 Git、文档或命令输出。

### 3. 项目仍是官方示例身份

当前 README、AGENTS、`app/main.py`、`app/config.yaml`、Compose service/image 名和 Python project metadata 仍以 AgentOS Docker template 和官方 Chief/Agent Builder/Platform Manager 为中心。

运行 setup 前至少需要决定：

- 正式平台名为 `Tidewise AgentOS`；
- 哪些官方组件仅用作参考、哪些保留；
- 第一个验收组件是 Tidewise 的代码定义 Workflow/Agent，而不是一个只保存在 Studio/Postgres 的临时组件；
- 生产 Agent、Team、Workflow 以 Git 中的代码为事实源。

## Wizard 未覆盖、但 Tidewise 初始化必须补齐

### 项目基线

- 重写 README 和 AGENTS 的项目所有权、非目标和交付边界；
- 记录为什么选择 Agno/AgentOS、Docker、Postgres 和 DeepSeek；
- 明确 AgentOS 拥有 Agent/Workflow/Schedule/Session/Trace，`tidewise-reason` 拥有 OpenSPG/KAG 推理；
- 两个仓库只通过明确 HTTP/DTO 契约交互，不共享数据库和实现 import。

### 可重现性与质量门

- 决定并固定 Agno 版本；模板当前固定 2.8.5，不应在无 smoke/eval 的情况下升级；
- 建立 `.env` 变量合同，实际值仍保密；
- 运行 format、lint、mypy 和 Docker build；
- 建立不需要外部 LLM 的基础 HTTP/Workflow smoke；
- 将模型行为评测与确定性工程测试分开。

### 安全与运行所有权

- 本地 API 和 Postgres 默认绑定 loopback；
- 生产环境使用 JWT/JWKS，不以 `RUNTIME_ENV=dev` 对外暴露；
- 明确 Trace/Session 的数据分类、保留周期与访问权限；
- 评估并显式设置 Agno telemetry 策略；
- 不把 Control Plane 当成本地 Runtime 的运行依赖。

## 不应照搬的 Wizard 内容

- Wizard 的“一次只问一个问题”和教学语气是 onboarding UX，不是工程規范。
- Wizard 默认使用 Agent Builder 创建第一个 Agent，但 Tidewise 生产定义应以 Git 代码为事实源。
- 当前仓库 `setup-platform` skill 默认推荐 Radar；它适合验证官方模板的教学流程，不是 Tidewise Collector 的产品需求。
- Wizard 建议使用直接 Agno constructor 风格，但模板本身已在 `app/settings.py` 使用 `default_model()`。仓库内已验证的约定应优先于 Wizard 的通用建议。

## 建议执行顺序

### Phase A：Tidewise 基线（先做）

1. 更新项目身份、AGENTS、README 和设计决策。
2. 决定保留/删除的官方示例组件。
3. 配置 DeepSeek 模型和对应环境变量。
4. 将 AgentOS Postgres 的宿主机端口与其他项目隔离。
5. 建立一个无 LLM 或可控模型的 Tidewise 验收组件。

### Phase B：执行本地 setup

1. 从 `example.env` 生成忽略的 `.env`，写入密钥但不输出。
2. `docker compose up -d --build`。
3. 等待 `/docs` 和 `/health` 就绪。
4. 运行 `scripts/mcp_check.sh`。
5. 连接 AgentOS UI Local endpoint。
6. 运行 Tidewise 验收 Workflow/Agent，查看 Session 与 Trace。
7. 执行 format/lint/mypy 和 smoke tests。

### Phase C：生产前

1. 固定生产域名与 `AGENTOS_URL`。
2. 配置 JWT/JWKS、MCP OAuth 和用户隔离。
3. 使用生产 Compose override，去掉 bind mount 和 hot reload。
4. 使用强数据库密码和密钥管理。
5. 建立备份、恢复、数据保留、容量与高可用性方案。

## 最终判断

我们对官方 wizard 的遗漏不是“少运行了一个 wizard 命令”，而是尚未执行模板自带的 setup 验收流程。但在运行它之前，必须先处理 Postgres 端口冲突、DeepSeek 模型选择和 Tidewise 项目身份。否则最多只能得到一个可运行的官方 Demo，而不是 Tidewise AgentOS 初始基线。
