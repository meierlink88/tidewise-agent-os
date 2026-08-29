# Tidewise AgentOS — Repository Instructions

本文件是本仓库的工程真源；`CLAUDE.md` 是指向它的符号链接。

## 项目定位

这是观潮家的独立 AgentOS 运行时项目，基于 Agno。Agent、Team、Workflow、Schedule 及其测试是核心交付物，必须由 Git 管理。Agno Control Plane 只是可选管理界面，不是本地 Agent 或调度执行的依赖。

## 架构

```text
AgentOS (app/main.py)
├── Tidewise Assistant (agents/tidewise_assistant.py)
├── Collection Query Planner (Agno Studio/PostgreSQL component; seeded by agents/raw_collector.py)
├── Collection Title Curator (Agno Studio/PostgreSQL component; seeded by agents/title_curator.py)
├── Evidence Extractor  (Agno Studio/PostgreSQL component; seeded by agents/evidence_extractor.py)
├── Event Extractor     (Agno Studio/PostgreSQL component; seeded by agents/event_extractor.py)
├── Local Ping         (workflows/local_ping.py)
├── Deployment Check   (workflows/deployment_check.py)
├── Raw Collection     (Agno Studio/PostgreSQL component; seeded by workflows/raw_collection.py)
├── Evidence Extraction (Agno Studio/PostgreSQL component; seeded by workflows/evidence_extraction.py)
└── Event Extraction    (Agno Studio/PostgreSQL component; seeded by workflows/event_extraction.py)
```

共享能力：

- `app/settings.py`：DeepSeek V4 Flash 模型工厂。
- `app/registry.py`：Studio 可见的安全 Registry。
- `db/session.py`：复用共享 PostgreSQL 实例中的独立 `agent_os` 数据库。
- `app/schedules.py`：显式一次性 Schedule seed 与启动只读校验。
- `app/config.yaml`：组件描述和快捷提示。
- `capabilities/collection/`：Collection 领域能力，仅以 `tools/`、`functions/`、`internal/` 三类目录组织。
- `capabilities/evidence/`：Evidence 领域能力，仅以 `tools/`、`functions/`、`internal/` 三类目录组织。
- `capabilities/event/`：Event Candidate 提炼与 Reasoning Server 交接能力，仅以 `tools/`、`functions/`、`internal/` 三类目录组织。
- `sematica/`：AgentOS 内部语义层模块，承载 Graphiti、Ontology、Event Analysis 与 Investment Reasoning 能力；它不是独立服务，不得自行启动第二个 HTTP 进程。
- `data/collector/`：本地原始采集 Artifact；目录受 Git 忽略。
- `data/evidence/`、`data/event/`：本地 Evidence 与 Event 提炼 Artifact；目录受 Git 忽略。

## 本地 Docker 约束

- Compose 项目名必须保持 `agent-os`，服务名保持 `agentos` 和 `neo4j`，本地容器名保持
  `agent-os` 和 `agent-os-neo4j`。
- 复用外部网络 `tidewise-local` 和服务别名 `postgres:5432`。
- 禁止在本仓库新增 PostgreSQL 服务；数据隔离使用数据库和角色完成。
- Neo4j 必须复用显式命名的 `tidewise-reason_graphiti-neo4j-data` 与
  `tidewise-reason_graphiti-neo4j-logs` 卷；未经用户明确授权禁止删除。
- 只使用带服务名的生命周期命令：`docker compose up -d --build agentos neo4j`、
  `stop agentos neo4j`、`rm -f agentos neo4j`。禁止执行 `docker compose down -v`。

## 组件规则

### Agent

每个 Agent 独立文件，使用 `default_model()` 和 `get_postgres_db()`；工具、记忆、知识库只按明确业务需求添加。Agent 适合开放式判断和对话，不负责可靠的固定工序编排。

### Workflow

固定采集、校验、去重、持久化、发布、补偿等工序使用 Workflow。Step 可以调用 Agent，但状态转移、幂等键、重试和副作用必须由确定性代码掌控。

### Capability

每项业务能力使用 `capabilities/<domain>/` 作为领域边界，且只有三类实现目录：`tools/` 只放供 Agent 自主调用的 Tool，`functions/` 只放供 Workflow 确定性调用的 Function，`internal/` 放合同、模型、Adapter、仓库、存储及 Tool/Function 共享实现。领域根 `__init__.py` 可显式导出稳定合同；其他领域、Agent、Workflow 和应用组装不得直接导入 `internal/`。`agents/` 只定义 Agent，`workflows/` 只定义 Workflow 编排、生命周期和 Studio seed；不得把业务 Function 实现直接写入 Workflow 文件。

### Team

只有在多个自治角色需要独立上下文、委派或协商时才使用 Team。不要为了表现“多 Agent”把顺序流水线改成 Team。

### Schedule

新环境通过 `python -m scripts.seed_schedules` 显式创建缺失的默认 Schedule。此后 PostgreSQL 与
Control Panel 拥有名称、cron、endpoint、payload 和启停状态；应用启动只按 Workflow endpoint
读取并告警，禁止创建、覆盖或恢复 Schedule。名称只是可编辑显示值，不是运行身份。同一必需
Workflow endpoint 出现多条 Schedule 必须视为配置错误，避免重复执行。

### 注册

新组件必须同时：

1. 添加独立源码文件。
2. 在 `app/main.py` 注册。
3. 在 `app/config.yaml` 添加 manifest。
4. 添加与风险匹配的测试或 eval。
5. 通过 REST 和 MCP 冒烟验证。

## 数据和密钥

- `.env` 不进 Git，不在日志或命令输出中显示密钥。
- 本地 DB：`postgres:5432/agent_os`，角色 `agent_os_runtime`。
- 本地 `RUNTIME_ENV=dev`；生产必须为 `prd` 并配置 JWT/JWKS。
- Studio 创建的组件属于运行时状态；准备进入生产的组件必须回写代码、评审并版本化。
- Raw Collector 与 Raw Collection 是显式混合组件：工具、Function 与运行合同由 Git 管理，当前发布的 Agent 提示词和 Workflow 编排由 Agno Studio 在 PostgreSQL 中版本化；每次运行加载当前发布版。

## 开发与验证

### Git 交付流程（强制）

任何代码、配置、文档或测试改动都必须完整经过 `Issue → Branch → Pull Request → 人工审核合并`：

1. **先创建 GitHub Issue**：在首次编辑仓库文件前创建 Issue，写明目标、范围和验收标准。没有 Issue 不得创建开发分支或产生改动。
2. **再创建开发分支**：确认工作区状态，从最新 `main` 创建与 Issue 关联的独立分支。Codex 使用 `codex/<issue-number>-<topic>`，其他工具使用 `<tool>/<issue-number>-<topic>`。
3. **完成后提交 Pull Request**：只在该分支完成实现、测试和范围清晰的提交，推送分支并创建关联 Issue 的 PR。PR 必须说明目标、关键改动、验证结果、风险和回滚方式。
4. **PR 由人工审核并合并**：Agent 可跟进 CI 和处理审核意见，但在 PR 可合并后必须停止并等待人工操作。Agent 禁止自行执行合并、开启自动合并、直接 push `main` 或以本地 merge 代替人工审核。
5. **人工合并后收尾**：确认 PR 已由人工合并后，同步本地 `main`，删除已完成的开发分支，再开始下一项工作。

若开始任务时发现自己位于 `main` 且任务需要产生改动，必须先创建 Issue，再创建开发分支，最后才能编辑文件；若工作区已有未提交改动，先查明归属并妥善保留，禁止通过重置或覆盖来清理。

### 验证命令

```bash
./scripts/venv_setup.sh
source .venv/bin/activate
./scripts/format.sh
./scripts/validate.sh

docker compose up -d --build agentos neo4j
curl -sSf http://localhost:8000/health
./scripts/mcp_check.sh
```

修改依赖后运行 `./scripts/generate_requirements.sh` 并重建镜像。修改普通 Python 文件时开发容器会热重载。

## 当前非目标

- 不在本仓库承载 OpenSPG/KAG 推理引擎实现。
- 不依赖 Agno 云端执行本地 Agent、Workflow 或 Schedule。
- 不把完整采集流程塞进单个 Agent 的自主 tool-calling 循环。
