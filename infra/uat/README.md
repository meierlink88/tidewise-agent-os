# AgentOS UAT on DGX Spark

## Deployment boundary

DGX Spark is the AgentOS UAT host. It runs three isolated Compose services:

```text
public HTTPS /agentos
        |
        v
DGX Spark (Linux ARM64)
├── AgentOS         127.0.0.1:9081 only
├── PostgreSQL 17   AgentOS runtime state only; no host port
└── Neo4j 5.26      AgentOS Graphiti only; no host port
        |
        +---- HTTPS + service token ----> Huawei Cloud public Data Service API
```

AgentOS never joins the Data Service Docker network and never receives Data Service PostgreSQL, MinIO, or other
infrastructure credentials. Raw Evidence content is submitted in the versioned Data Service API request; Data Service
owns its persistence. PostgreSQL and Neo4j on DGX are dedicated AgentOS dependencies and use explicit named volumes.

The current Compose binds AgentOS only to `127.0.0.1:9081`. Before cutover, provision a reviewed TLS route or tunnel
from the public `AGENTOS_EXTERNAL_URL` to that loopback listener. A release is accepted only when public `/health`
returns the candidate commit in `X-Tidewise-Release`; this prevents a successful check against the old ECS instance.

## Security and persistence

- AgentOS runs as UID/GID `10002:10002`, read-only root filesystem, no Linux capabilities, and no new privileges.
- PostgreSQL and Neo4j have no published ports. Only AgentOS can reach them on `tidewise-agentos-uat-private`.
- The Data Service base URL must be public HTTPS. Preflight rejects loopback/private DNS results and verifies an
  authenticated, complete Source Snapshot through the candidate image.
- All three images are immutable digest references and must resolve to `linux/arm64` on DGX.
- PostgreSQL, Neo4j data, and Neo4j logs use `tidewise-agentos-uat-*` named volumes. Never run `docker compose down -v`.
- Before an upgrade of an existing DGX release, deployment writes a compressed PostgreSQL dump under
  `/opt/tidewise/agentos-uat/backups`. Back up the three named volumes to independent storage before host maintenance.
- Runtime secrets live in the GitHub `uat` Environment and a mode `0600` temporary env file; they are not in images.

## One-time DGX bootstrap

DGX already owns its NVIDIA Docker installation. The bootstrap validates Docker/Compose and does not replace them.
Download and checksum the official ARM64 GitHub Actions runner archive, then run:

```bash
sudo env \
  UAT_RUNNER_NAME=tidewise-agentos-uat-dgx \
  GITHUB_REPOSITORY_URL=https://github.com/meierlink88/tidewise-agent-os \
  GITHUB_RUNNER_REGISTRATION_TOKEN='<short-lived-token>' \
  ACTIONS_RUNNER_ARCHIVE=/tmp/actions-runner-linux-arm64.tar.gz \
  ACTIONS_RUNNER_ARCHIVE_SHA256='<verified-sha256>' \
  bash infra/uat/bootstrap-dgx.sh
```

The runner is registered with `tidewise-agentos-uat-dgx`. Bootstrap creates the deploy, state, backup, artifact, and
JWKS paths but does not start a release.

## GitHub `uat` configuration

Variables:

- `SWR_REGISTRY`, `SWR_NAMESPACE`, `SWR_AGENTOS_REPOSITORY`, `SWR_AGENTOS_DEPLOY_REPOSITORY`
- `UAT_AGENTOS_RUNNER_NAME` — `tidewise-agentos-uat-dgx`
- `AGENTOS_EXTERNAL_URL` — public HTTPS issuer/base, normally `https://tideai.tripwise.cn/agentos`
- `DATA_SERVICE_BASE_URL` — Huawei Cloud public API base, normally `https://tideai.tripwise.cn`
- `GRAPHITI_EMBEDDING_BASE_URL`, `GRAPHITI_EMBEDDING_MODEL`, `GRAPHITI_EMBEDDING_DIM`
- `EVENT_EXTRACTION_BATCH_SIZE` and `CONTROL_PLANE_JWT_VERIFICATION_KEY`

Secrets:

- `SWR_USERNAME`, `SWR_PASSWORD`, `SWR_PULL_USERNAME`, `SWR_PULL_PASSWORD`
- `AGENTOS_DB_PASSWORD`, `NEO4J_PASSWORD`, `DEEPSEEK_API_KEY`, `DATA_SERVICE_TOKEN`
- `GRAPHITI_EMBEDDING_API_KEY`, `JWT_JWKS_BASE64`
- optional `MCP_CONNECT_SECRET`, `AGENTOS_MCP_SIGNING_KEY`

No Data Service database or object-storage variable/secret belongs in this environment.

## State migration and cutover

1. Keep the ECS AgentOS running and create a restorable AgentOS-only PostgreSQL dump through the old private runner.
2. Transfer the encrypted dump to `/opt/tidewise/agentos-uat/backups`; verify its checksum before decrypting locally.
3. Start only DGX PostgreSQL and restore the dump as `agent_os_uat_runtime`; do not copy Data Service databases.
4. Start DGX Neo4j as a new AgentOS-exclusive graph. Populate authoritative graph inputs through supported AgentOS
   commands/API paths; do not copy or mount the legacy OpenSPG/Data Service Neo4j volumes.
5. Dispatch **Deploy UAT** with `stage_only` enabled. This proves internal health, auth, components, workflows,
   schedules, MCP, Data API, Graphiti, restart recovery, volume persistence, and image digests without claiming cutover.
6. Route public HTTPS `/agentos` to DGX and dispatch the same commit with `stage_only` disabled. The candidate SHA
   header must match before the release is accepted.
7. Observe the DGX release before retiring the ECS AgentOS. Retirement is a separate, explicitly authorized action.

Run **Deploy UAT** manually from `main`. It accepts only a commit already validated on `main`, builds ARM64 images on
DGX, deploys digest references, starts dedicated PostgreSQL and Neo4j, applies the additive Agno migration, and seeds
default schedules only for the first DGX release. On candidate failure, it restores the prior AgentOS image/config;
database state is not automatically rolled back. The last two release snapshots remain under the deployment state
directory.
