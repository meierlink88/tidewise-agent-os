# AgentOS UAT Deployment

## Topology

AgentOS is an independently deployed release unit on the same Huawei Cloud ECS used by Tidewise AI.
It reuses the Huawei SWR registry, the PostgreSQL RDS instance, and the external `tidewise-uat`
Docker network, while keeping its database, role, Compose project, data, runner state, and rollback state isolated.

```text
Huawei ECS
├── tidewise-uat                    # Tidewise AI Compose; data is internal at data:9011
└── tidewise-agentos-uat            # this repository; AgentOS is fixed at 9081

Huawei RDS PostgreSQL (private VPC endpoint only)
└── agent_os_uat / agent_os_uat_runtime
```

The UAT port contract lives only in `docker-compose.yaml`: Uvicorn listens on `9081`, the host publishes
`9081`, and the healthcheck uses `9081`. `AGENTOS_EXTERNAL_URL` is the operator/Control Plane address;
`AGENTOS_INTERNAL_URL` is fixed to `http://127.0.0.1:9081` for Scheduler callbacks.

## Security boundary

- RDS has no public endpoint. Its allowlist/security group permits PostgreSQL `5432` only from the ECS private address.
- The AgentOS role owns only `agent_os_uat`; `DB_SSLMODE=require` is mandatory.
- Data Service has no host port. AgentOS calls `http://data:9011` through `tidewise-uat` using a service token.
- AgentOS runs as UID/GID `10002:10002`, read-only root filesystem, dropped Linux capabilities, and `no-new-privileges`.
- ECS security group permits public `9081` only from approved operator source addresses. JWT authorization remains mandatory.
- Runtime credentials are GitHub `uat` Secrets, written only to a mode `0600` temporary env file. They are never baked into images.
- The JWKS is supplied as a base64 secret, decoded to `/opt/tidewise/agentos-uat/jwt-jwks.json` mode `0640` for the dedicated runtime group, and mounted read-only.

## One-time RDS initialization

Run as the RDS administrative owner through a private connection. Supply the password through the SQL client; do not
put it in shell history or this repository.

```sql
CREATE ROLE agent_os_uat_runtime LOGIN;
CREATE DATABASE agent_os_uat OWNER agent_os_uat_runtime;
REVOKE ALL ON DATABASE agent_os_uat FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE agent_os_uat TO agent_os_uat_runtime;
```

Set a strong password separately and confirm RDS automated backups/PITR before the first deployment. AgentOS provisions
its own Agno tables on first boot; the deployment never runs a down migration or restores RDS automatically.

## One-time ECS bootstrap

The Tidewise AI `tidewise-deploy` user and `/opt/tidewise/uat` release root must already exist. Register a separate
repository runner because the existing Tidewise AI runner is repository-scoped.

```bash
sudo env \
  UAT_RUNNER_NAME=tidewise-agentos-uat-ecs \
  GITHUB_REPOSITORY_URL=https://github.com/meierlink88/tidewise-agent-os \
  GITHUB_RUNNER_REGISTRATION_TOKEN='<short-lived-token>' \
  ACTIONS_RUNNER_ARCHIVE=/tmp/actions-runner.tar.gz \
  ACTIONS_RUNNER_ARCHIVE_SHA256='<verified-sha256>' \
  bash infra/uat/bootstrap-ecs.sh
```

The runner label is `tidewise-agentos-uat-ecs`; daily deployment does not require root or SSH access.
Bootstrap registers it with GitHub's supported `--disableupdate` mode because large downloads from GitHub are slow on
this ECS. Upgrade the runner archive explicitly during a maintenance window before its version falls out of support.

## GitHub `uat` configuration

Environment or repository Variables:

- `SWR_REGISTRY`, `SWR_NAMESPACE`
- `SWR_AGENTOS_REPOSITORY`, `SWR_AGENTOS_DEPLOY_REPOSITORY`
- `UAT_AGENTOS_RUNNER_NAME`
- `AGENTOS_EXTERNAL_URL` — currently `http://<ECS-public-IP>:9081`
- `RDS_HOST` — Huawei RDS private hostname

Secrets:

- `SWR_USERNAME`, `SWR_PASSWORD` — push credentials
- `SWR_PULL_USERNAME`, `SWR_PULL_PASSWORD` — ECS read-only credentials
- `AGENTOS_DB_PASSWORD`, `DEEPSEEK_API_KEY`, `DATA_SERVICE_TOKEN`
- `PARALLEL_API_KEY`, `TAVILY_API_KEY`, `BOCHA_API_KEY`
- `JWT_JWKS_BASE64`
- optional `MCP_CONNECT_SECRET`, `AGENTOS_MCP_SIGNING_KEY`

Create the JWKS secret without printing it:

```bash
base64 < agentos-uat-jwks.json | gh secret set JWT_JWKS_BASE64 --env uat
```

## Release and rollback

Run **Deploy UAT** manually from the `main` workflow ref. The workflow validates that the chosen commit belongs to
`main` and has a successful `Validate` run, builds AMD64 images on a GitHub-hosted runner, pushes them to SWR, and
deploys immutable digest references on the ECS runner.

GitHub concurrency prevents two AgentOS deploy jobs. The ECS script also locks both
`/opt/tidewise/uat/deploy.lock` and `/opt/tidewise/agentos-uat/deploy.lock`, preventing overlap with Tidewise AI.
After deployment it verifies external health/auth, Agents, Workflows, Schedules, `local-ping`, MCP, and restart recovery.

If verification fails, the previous successful image/runtime/Compose snapshot is restored automatically. Database
state is not rolled back. The last two successful release identities remain under `/opt/tidewise/agentos-uat/state/`.
