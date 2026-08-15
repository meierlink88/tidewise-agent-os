# AgentOS UAT Deployment

## Topology

AgentOS is an independently deployed release unit on the same Huawei Cloud ECS used by Tidewise AI.
It reuses the Huawei SWR registry, the PostgreSQL RDS instance, and the external `tidewise-uat`
Docker network, while keeping its database, role, Compose project, data, runner state, and rollback state isolated.
Raw Evidence publication also uses a pre-provisioned MinIO endpoint reachable from `tidewise-uat`; its
`raw-evidence` bucket must allow direct browser downloads through the environment's public MinIO Base URL.
Preflight writes one content-addressed canary through authenticated S3, reads it anonymously through that public Base
URL with Markdown/inline response headers, and removes only that canary before deployment continues.

```text
Huawei ECS
├── Nginx :443                     # https://tideai.tripwise.cn/agentos/
├── tidewise-uat                    # Tidewise AI Compose; data is internal at data:9011
└── tidewise-agentos-uat            # AgentOS is loopback-only at 127.0.0.1:9081

Huawei RDS PostgreSQL (private VPC endpoint only)
└── agent_os_uat / agent_os_uat_runtime
```

The UAT port contract lives in `docker-compose.yaml`: Uvicorn listens on `9081`, Docker binds it only to
`127.0.0.1:9081`, and the healthcheck uses that port. Shared Nginx terminates TLS and exposes
`https://tideai.tripwise.cn/agentos`; `AGENTOS_INTERNAL_URL` remains `http://127.0.0.1:9081` for Scheduler callbacks.
The same Nginx snippet also routes the RFC 9728 protected-resource and RFC 8414 authorization-server discovery URLs
derived from the `/agentos/mcp` resource and `/agentos` issuer.
Deployment checks resolve that public hostname to `127.0.0.1` on the ECS so they exercise local Nginx with normal TLS
SNI and certificate verification without depending on unsupported public-IP NAT hairpin behavior.

## Security boundary

- RDS has no public endpoint. Its allowlist/security group permits PostgreSQL `5432` only from the ECS private address.
- The AgentOS role owns only `agent_os_uat`; `DB_SSLMODE=require` is mandatory.
- Data Service has no host port. AgentOS calls `http://data:9011` through `tidewise-uat` using a service token.
- AgentOS runs as UID/GID `10002:10002`, read-only root filesystem, dropped Linux capabilities, and `no-new-privileges`.
- Docker does not publish `9081` on a public interface. Existing Nginx `443` is the only public AgentOS ingress.
- JWT authorization remains mandatory. MCP OAuth is enabled only with an HTTPS issuer.
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

Install the versioned AgentOS location into the existing `tideai.tripwise.cn` HTTPS server before the first release:

```bash
sudo install -m 0644 infra/uat/nginx-agentos-location.conf \
  /etc/nginx/snippets/tidewise-agentos-uat.conf
```

Add the following line inside the existing `listen 443 ssl` server block, then validate and reload Nginx:

```nginx
include /etc/nginx/snippets/tidewise-agentos-uat.conf;
```

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -sS -D - -o /dev/null https://tideai.tripwise.cn/agentos/health \
  | grep -i '^X-Tidewise-Upstream: agentos-uat'
```

## GitHub `uat` configuration

Environment or repository Variables:

- `SWR_REGISTRY`, `SWR_NAMESPACE`
- `SWR_AGENTOS_REPOSITORY`, `SWR_AGENTOS_DEPLOY_REPOSITORY`
- `UAT_AGENTOS_RUNNER_NAME`
- `AGENTOS_EXTERNAL_URL` — `https://tideai.tripwise.cn/agentos`
- `RDS_HOST` — Huawei RDS private hostname
- `MINIO_ENDPOINT` — MinIO S3 API URL reachable from containers, including `http://` or `https://`
- `RAW_EVIDENCE_PUBLIC_BASE_URL` — browser-facing MinIO Base URL; deployment verifies a canary through it
- `CONTROL_PLANE_JWT_VERIFICATION_KEY` — PEM public key generated for the UAT OS connection in Agno Control Plane

Secrets:

- `SWR_USERNAME`, `SWR_PASSWORD` — push credentials
- `SWR_PULL_USERNAME`, `SWR_PULL_PASSWORD` — ECS read-only credentials
- `AGENTOS_DB_PASSWORD`, `DEEPSEEK_API_KEY`, `DATA_SERVICE_TOKEN`
- `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`
- `PARALLEL_API_KEY`, `TAVILY_API_KEY`, `BOCHA_API_KEY`
- `JWT_JWKS_BASE64`
- optional `MCP_CONNECT_SECRET`, `AGENTOS_MCP_SIGNING_KEY`

Create the JWKS secret without printing it:

```bash
base64 < agentos-uat-jwks.json | gh secret set JWT_JWKS_BASE64 --env uat
```

The Control Plane public key is additive: Agno validates its JWTs with `JWT_VERIFICATION_KEY` while retaining
`JWT_JWKS_FILE` for existing identity-provider keys. Store only the public key as the UAT Environment variable;
never store the corresponding private signing key in this repository or on the AgentOS host.

## Release and rollback

Run **Deploy UAT** manually from the `main` workflow ref. The workflow validates that the chosen commit belongs to
`main` and has a successful `Validate` run, builds AMD64 images on a GitHub-hosted runner, pushes them to SWR, and
deploys immutable digest references on the ECS runner.

GitHub concurrency prevents two AgentOS deploy jobs. The ECS script also locks both
`/opt/tidewise/uat/deploy.lock` and `/opt/tidewise/agentos-uat/deploy.lock`, preventing overlap with Tidewise AI.
On the first deployment only, the ECS script explicitly seeds missing Schedule defaults before verification. Later
deployments and application restarts preserve PostgreSQL/Control Panel Schedule configuration. After deployment it
verifies external health/auth, Agents, Workflows, unique required Schedule endpoints, `local-ping`, MCP, and restart
recovery.

If verification fails, the previous successful image/runtime/Compose snapshot is restored automatically. Database
state is not rolled back. Candidate logs are sanitized and captured before rollback removes the container. The last
two successful release identities remain under `/opt/tidewise/agentos-uat/state/`.
