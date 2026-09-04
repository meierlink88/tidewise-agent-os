import subprocess
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from agno.os import AgentOSBuiltinAuth
from agno.os.middleware.user_scope import get_scoped_user_id
from agno.os.scopes import has_required_scopes
from fastmcp import FastMCP
from starlette.requests import Request

from scripts import smoke_uat
from scripts.smoke_uat import UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES, UAT_SMOKE_SERVICE_ACCOUNT_SCOPES

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class UatIngressContractTest(TestCase):
    def test_failure_diagnostics_redact_neo4j_auth_values(self) -> None:
        diagnostics = REPOSITORY_ROOT / "infra/uat/collect-diagnostics.sh"
        leaked_value = "neo4j/unsafe-example-value"

        result = subprocess.run(
            [diagnostics, "--redact-stdin"],
            input=(
                f"NEO4J_AUTH={leaked_value}\n"
                f"Invalid value for NEO4J_AUTH: '{leaked_value}'\n"
                f"{leaked_value} is invalid\n"
            ),
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("unsafe-example-value", result.stdout)
        self.assertEqual(result.stdout.count("[REDACTED]"), 3)

    def test_failure_diagnostics_redact_minio_credentials(self) -> None:
        diagnostics = REPOSITORY_ROOT / "infra/uat/collect-diagnostics.sh"
        result = subprocess.run(
            [diagnostics, "--redact-stdin"],
            input="MINIO_ACCESS_KEY=unsafe-user\nMINIO_SECRET_KEY=unsafe-password\n",
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertNotIn("unsafe-user", result.stdout)
        self.assertNotIn("unsafe-password", result.stdout)
        self.assertEqual(result.stdout.count("[REDACTED]"), 2)

    def test_deployment_probe_has_its_required_scopes(self) -> None:
        required_scopes = [
            "agents:read",
            "workflows:read",
            "workflows:run",
            "config:read",
        ]

        self.assertTrue(
            has_required_scopes(
                UAT_SMOKE_SERVICE_ACCOUNT_SCOPES,
                required_scopes,
            )
        )
        self.assertNotIn("agent_os:admin", UAT_SMOKE_SERVICE_ACCOUNT_SCOPES)

    def test_schedule_probe_is_unscoped_only_with_admin_scope(self) -> None:
        regular_request = Request({"type": "http"})
        regular_request.state.user_id = "sa:uat-deploy-smoke"
        regular_request.state.scopes = list(UAT_SMOKE_SERVICE_ACCOUNT_SCOPES)
        self.assertEqual(get_scoped_user_id(regular_request), "sa:uat-deploy-smoke")

        schedule_request = Request({"type": "http"})
        schedule_request.state.user_id = "sa:uat-schedule-probe"
        schedule_request.state.scopes = list(UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES)
        self.assertIsNone(get_scoped_user_id(schedule_request))
        self.assertEqual(UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES, ["agent_os:admin"])

    def test_dgx_owns_agentos_dependencies_and_uses_public_data_api(self) -> None:
        compose = (REPOSITORY_ROOT / "infra/uat/docker-compose.yaml").read_text()
        example_env = (REPOSITORY_ROOT / "infra/uat/.env.example").read_text()
        preflight = (REPOSITORY_ROOT / "infra/uat/preflight.sh").read_text()
        deploy_dependencies = (REPOSITORY_ROOT / "infra/uat/deploy-dependencies.sh").read_text()
        deploy = (REPOSITORY_ROOT / "infra/uat/deploy.sh").read_text()
        port_verifier = REPOSITORY_ROOT / "infra/uat/verify-dependency-ports.py"
        entrypoint = (REPOSITORY_ROOT / "scripts/entrypoint.sh").read_text()
        workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-uat.yml").read_text()

        self.assertIn('"127.0.0.1:9081:9081"', compose)
        self.assertIn("postgres:", compose)
        self.assertIn("neo4j:", compose)
        self.assertIn("minio:", compose)
        neo4j_service = compose.split("  neo4j:", 1)[1].split("  agentos:", 1)[0]
        self.assertNotIn("\n      NEO4J_PASSWORD:", neo4j_service)
        self.assertIn("$${NEO4J_AUTH#*/}", neo4j_service)
        self.assertIn("tidewise-agentos-uat-postgres-data", compose)
        self.assertIn("tidewise-agentos-uat-neo4j-data", compose)
        self.assertIn("published: ${POSTGRES_LAN_PORT:?POSTGRES_LAN_PORT is required}", compose)
        self.assertIn("published: ${NEO4J_HTTP_LAN_PORT:?NEO4J_HTTP_LAN_PORT is required}", compose)
        self.assertIn("published: ${NEO4J_BOLT_LAN_PORT:?NEO4J_BOLT_LAN_PORT is required}", compose)
        self.assertEqual(compose.count("host_ip: ${UAT_LAN_BIND_ADDRESS:?UAT_LAN_BIND_ADDRESS is required}"), 4)
        self.assertIn("host_ip: 127.0.0.1", compose)
        self.assertIn(
            "AGENTOS_EXTERNAL_URL=https://tideai.tripwise.cn/agentos",
            example_env,
        )
        self.assertIn("DATA_SERVICE_BASE_URL=https://tideai.tripwise.cn", example_env)
        self.assertIn("public Data Service Source Snapshot", preflight)
        self.assertIn("is_private", preflight)
        self.assertNotIn("--insecure", preflight)
        self.assertNotIn("--insecure", deploy)
        self.assertIn("MINIO_ENDPOINT: http://minio:9000", compose)
        self.assertIn("MINIO_ACCESS_KEY", compose)
        self.assertIn("RAW_EVIDENCE_PUBLIC_BASE_URL", compose)
        self.assertIn("MINIO_IMAGE", preflight)
        self.assertIn("raw-evidence-public-url", preflight)
        self.assertIn("MINIO_IMAGE", workflow)
        self.assertIn("MINIO_ACCESS_KEY: ${{ secrets.MINIO_ACCESS_KEY }}", workflow)
        self.assertIn("RAW_EVIDENCE_PUBLIC_BASE_URL: ${{ vars.RAW_EVIDENCE_PUBLIC_BASE_URL }}", workflow)
        self.assertNotIn("RDS_HOST", workflow)
        self.assertIn("DATA_SERVICE_BASE_URL: ${{ vars.DATA_SERVICE_BASE_URL }}", workflow)
        self.assertIn("UAT_LAN_BIND_ADDRESS: ${{ vars.UAT_LAN_BIND_ADDRESS }}", workflow)
        self.assertIn("POSTGRES_LAN_PORT: ${{ vars.POSTGRES_LAN_PORT }}", workflow)
        self.assertIn("NEO4J_HTTP_LAN_PORT: ${{ vars.NEO4J_HTTP_LAN_PORT }}", workflow)
        self.assertIn("NEO4J_BOLT_LAN_PORT: ${{ vars.NEO4J_BOLT_LAN_PORT }}", workflow)
        self.assertIn("MINIO_LAN_PORT: ${{ vars.MINIO_LAN_PORT }}", workflow)
        self.assertIn("MINIO_CONSOLE_PORT: ${{ vars.MINIO_CONSOLE_PORT }}", workflow)
        self.assertIn("UAT_LAN_BIND_ADDRESS=192.168.0.53", example_env)
        self.assertIn("POSTGRES_LAN_PORT=15432", example_env)
        self.assertIn(
            "JWT_VERIFICATION_KEY: ${JWT_VERIFICATION_KEY:?JWT_VERIFICATION_KEY is required}",
            compose,
        )
        self.assertIn(
            "CONTROL_PLANE_JWT_VERIFICATION_KEY: ${{ vars.CONTROL_PLANE_JWT_VERIFICATION_KEY }}",
            workflow,
        )
        self.assertIn("openssl pkey -pubin -noout", workflow)
        self.assertIn('lines.append(f"JWT_VERIFICATION_KEY={json.dumps(verification_key)}")', workflow)
        self.assertIn('if [ ! -s "$current_sha" ]; then', deploy)
        self.assertIn("python -m scripts.seed_schedules", deploy)
        first_release = deploy.split(
            'migrate_candidate_database "$runtime_env" "$candidate_images" "$candidate_compose"',
            1,
        )[1].split('verify_release "$runtime_env"', 1)[0]
        self.assertLess(first_release.index("python -m scripts.seed_schedules"), first_release.index("up -d --wait"))
        self.assertIn("run --rm --no-deps agentos", first_release)
        self.assertNotIn("dockerize", entrypoint)
        self.assertIn("socket.create_connection", entrypoint)
        self.assertIn("time.monotonic() + 300", entrypoint)
        self.assertIn(
            "NEO4J_URI: bolt://neo4j:7687",
            compose,
        )
        self.assertIn(
            "NEO4J_PASSWORD: ${NEO4J_PASSWORD:?NEO4J_PASSWORD is required}",
            compose,
        )
        self.assertNotIn("GRAPHITI_LLM_", compose)
        self.assertIn(
            "GRAPHITI_EMBEDDING_API_KEY: ${GRAPHITI_EMBEDDING_API_KEY:?GRAPHITI_EMBEDDING_API_KEY is required}",
            compose,
        )
        self.assertIn("EVENT_ARTIFACT_ROOT: /app/data/event", compose)
        self.assertIn("INVESTMENT_ARTIFACT_ROOT: /app/data/investment", compose)
        self.assertIn("NEO4J_PASSWORD: ${{ secrets.NEO4J_PASSWORD }}", workflow)
        self.assertIn("GRAPHITI_EMBEDDING_API_KEY: ${{ secrets.GRAPHITI_EMBEDDING_API_KEY }}", workflow)
        self.assertIn("EVENT_EXTRACTION_BATCH_SIZE: ${{ vars.EVENT_EXTRACTION_BATCH_SIZE || '20' }}", workflow)
        self.assertIn('"EVENT_EXTRACTION_BATCH_SIZE",', workflow)
        self.assertIn("-m sematica.graphiti.readiness", deploy)
        self.assertIn("x-tidewise-release", deploy)
        self.assertIn('stage_only="${STAGE_ONLY:-false}"', deploy)
        self.assertIn("dependencies_only:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("environment: uat", workflow)
        self.assertIn("Require successful validation for the commit", workflow)
        self.assertIn("Build and deploy on UAT DGX Spark", workflow)
        self.assertIn("Deploy and verify fresh dependencies", workflow)
        self.assertIn("inputs.dependencies_only", workflow)
        self.assertIn("Build immutable AgentOS image locally on DGX", workflow)
        self.assertIn("docker buildx build", workflow)
        self.assertIn("--platform linux/arm64", workflow)
        self.assertIn("--load", workflow)
        self.assertIn('echo "AGENTOS_IMAGE=$image_id" >> "$GITHUB_ENV"', workflow)
        self.assertIn("org.opencontainers.image.revision", workflow)
        self.assertIn('{{index .Config.Labels "org.opencontainers.image.revision"}}', workflow)
        self.assertNotIn(r"{{index .Config.Labels \"org.opencontainers.image.revision\"}}", workflow)
        self.assertNotIn("SWR_", workflow)
        self.assertNotIn("docker/login-action", workflow)
        self.assertNotIn("docker/build-push-action", workflow)
        self.assertNotIn("docker push", workflow)
        self.assertNotIn("deploy-bundle", workflow)
        self.assertNotIn("SWR_", preflight)
        self.assertIn("AGENTOS_IMAGE must be a local image ID", preflight)
        self.assertIn("AGENTOS_IMAGE does not match RELEASE_SHA", preflight)
        self.assertIn("up -d --wait --wait-timeout 240 postgres neo4j minio", deploy_dependencies)
        self.assertIn("pg_control_system()", deploy_dependencies)
        self.assertIn("SHOW DATABASES YIELD name, databaseID", deploy_dependencies)
        self.assertIn("verify_raw_evidence_storage", deploy_dependencies)
        self.assertIn("tidewise-agentos-uat-minio-data /data", deploy_dependencies)
        self.assertIn("verify-dependency-ports.py", deploy_dependencies)
        self.assertIn("verify-dependency-ports.py", deploy)
        self.assertTrue(port_verifier.stat().st_mode & 0o111)
        self.assertIn("actual_postgres != expected_postgres", port_verifier.read_text())
        self.assertIn("AgentOS was started", deploy_dependencies)
        self.assertNotIn("down -v", deploy_dependencies)
        self.assertIn("pg_dump", deploy)
        self.assertIn(
            'verify_release "$current_runtime" "$current_images" "$current_compose" "$(cat "$current_sha")" false',
            deploy,
        )
        self.assertIn("linux/arm64", workflow)
        self.assertIn("tidewise-agentos-uat-dgx", workflow)
        self.assertIn("postgres:17.11-bookworm@sha256:", workflow)
        self.assertIn("minio/minio:RELEASE.2025-07-23T15-54-02Z@sha256:", workflow)
        self.assertNotIn("postgres:16@sha256:", workflow)
        self.assertFalse((REPOSITORY_ROOT / ".github/workflows/migrate-uat-state.yml").exists())
        self.assertFalse((REPOSITORY_ROOT / "infra/uat/deploy-bundle.Dockerfile").exists())
        self.assertNotIn("REASON_SERVICE", compose)
        self.assertNotIn("REASON_SERVICE", workflow)
        self.assertIn(
            'session.call_tool("get_agentos_config", {})', (REPOSITORY_ROOT / "scripts/smoke_uat.py").read_text()
        )

    def test_mcp_oauth_accepts_https_path_issuer(self) -> None:
        auth = AgentOSBuiltinAuth(
            url="https://tideai.tripwise.cn/agentos",
            secret="x" * 32,
            signing_key_material="y" * 32,
        )

        FastMCP(name="uat-contract", auth=auth).http_app(path="/mcp")
        paths = {route.path for route in auth.get_routes("/mcp")}

        self.assertIn("/.well-known/oauth-authorization-server", paths)
        self.assertIn("/.well-known/oauth-protected-resource/agentos/mcp", paths)

    def test_mcp_oauth_rejects_http_issuer(self) -> None:
        auth = AgentOSBuiltinAuth(
            url="http://123.60.99.198:9081",
            secret="x" * 32,
            signing_key_material="y" * 32,
        )

        with self.assertRaisesRegex(ValueError, "Issuer URL must be HTTPS"):
            FastMCP(name="uat-contract", auth=auth).http_app(path="/mcp")


class UatSmokeCleanupTest(IsolatedAsyncioTestCase):
    async def test_removes_both_temporary_accounts_when_probe_fails(self) -> None:
        class FakeDb:
            def __init__(self) -> None:
                self.created: list[dict] = []
                self.deleted: list[str] = []

            def create_service_account(self, account: dict) -> None:
                self.created.append(account)

            def delete_service_account(self, account_id: str) -> None:
                self.deleted.append(account_id)

        db = FakeDb()
        probe = AsyncMock(side_effect=RuntimeError("probe failed"))

        with patch.object(smoke_uat, "get_postgres_db", return_value=db), patch.object(smoke_uat, "_probe", probe):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                await smoke_uat.main()

        self.assertEqual(len(db.created), 2)
        self.assertEqual({account["id"] for account in db.created}, set(db.deleted))
        self.assertEqual(db.created[0]["scopes"], UAT_SMOKE_SERVICE_ACCOUNT_SCOPES)
        self.assertEqual(db.created[1]["scopes"], UAT_SCHEDULE_PROBE_SERVICE_ACCOUNT_SCOPES)
        probe.assert_awaited_once()
