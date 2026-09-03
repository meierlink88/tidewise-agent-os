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
returns the candidate commit in `X-Tidewise-Release`; this prevents a successful check against an unintended backend.

## Security and persistence

- AgentOS runs as UID/GID `10002:10002`, read-only root filesystem, no Linux capabilities, and no new privileges.
- PostgreSQL and Neo4j have no published ports. Only AgentOS can reach them on `tidewise-agentos-uat-private`.
- The Data Service base URL must be public HTTPS. Preflight rejects loopback/private DNS results and verifies an
  authenticated, complete Source Snapshot through the candidate image.
- AgentOS is built on DGX from the validated `main` commit and Compose receives its immutable local image ID.
  PostgreSQL and Neo4j remain pinned by registry digest. All three images must resolve to `linux/arm64` on DGX.
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

- `UAT_AGENTOS_RUNNER_NAME` — `tidewise-agentos-uat-dgx`
- `AGENTOS_EXTERNAL_URL` — public HTTPS issuer/base, normally `https://tideai.tripwise.cn/agentos`
- `DATA_SERVICE_BASE_URL` — Huawei Cloud public API base, normally `https://tideai.tripwise.cn`
- `GRAPHITI_EMBEDDING_BASE_URL`, `GRAPHITI_EMBEDDING_MODEL`, `GRAPHITI_EMBEDDING_DIM`
- `EVENT_EXTRACTION_BATCH_SIZE` and `CONTROL_PLANE_JWT_VERIFICATION_KEY`

Secrets:

- `AGENTOS_DB_PASSWORD`, `NEO4J_PASSWORD`, `DEEPSEEK_API_KEY`, `DATA_SERVICE_TOKEN`
- `GRAPHITI_EMBEDDING_API_KEY`, `JWT_JWKS_BASE64`
- optional `MCP_CONNECT_SECRET`, `AGENTOS_MCP_SIGNING_KEY`

No Data Service database or object-storage variable/secret belongs in this environment.

## Fresh initialization and cutover

The DGX environment is a new UAT and never imports the legacy ECS database or Artifact directory. "Copy development"
means promoting the Git-tracked Agent, Workflow, capability, manifest, and Schedule definitions after they have been
reviewed and merged into `main`; it never copies a developer database, `.env`, secrets, or ignored local Artifacts.

1. Merge the development change into `main` and wait for that exact commit's **Validate** run to succeed.
2. Dispatch **Deploy UAT** from `main` with `dependencies_only=true`. This starts and verifies only fresh PostgreSQL
   and AgentOS-exclusive Neo4j, proves named-volume persistence across restart, and proves that neither service
   publishes a host port. AgentOS remains stopped.
3. Dispatch the same validated commit with `dependencies_only=false` and `stage_only=true`. This deploys AgentOS,
   applies the additive Agno migration to the fresh database, seeds code-defined default Schedules, and proves internal
   health, auth, components, workflows, MCP, public Data API access, Graphiti, restart recovery, and image identity.
   The smoke check keeps workflow execution on a least-privilege temporary service account and uses a separate
   five-minute administrator probe only to verify the unowned system Schedules through the authenticated API;
   both accounts are deleted even when verification fails.
4. Route public HTTPS `/agentos` to DGX and dispatch the same commit with `stage_only=false`. The candidate SHA header
   must match before the release is accepted.
5. Observe the DGX release. Any legacy ECS cleanup is outside this deployment and requires separate authorization.

Run **Deploy UAT** manually from `main`. It accepts only a commit already validated on `main`, builds ARM64 images on
DGX without pushing to or pulling from an AgentOS registry, resolves the result to a local Docker image ID, starts
dedicated PostgreSQL and Neo4j, applies the additive Agno migration, and seeds default schedules only for the first DGX
release. The deployment scripts come directly from the same checked-out release commit; there is no deployment-bundle
image. On candidate failure, it restores the prior AgentOS image/config; database state is not automatically rolled
back. Local release images must be retained for rollback. The last two release snapshots remain under the deployment
state directory. In `dependencies_only` mode it records only the dependency image and Compose snapshot; it does not
create an AgentOS release snapshot or start the application.
