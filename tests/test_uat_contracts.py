from pathlib import Path
from unittest import TestCase

from agno.os import AgentOSBuiltinAuth
from agno.os.scopes import has_required_scopes
from fastmcp import FastMCP

from scripts.smoke_uat import UAT_SMOKE_SERVICE_ACCOUNT_SCOPES

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class UatIngressContractTest(TestCase):
    def test_deployment_probe_has_its_required_scopes(self) -> None:
        required_scopes = [
            "agents:read",
            "workflows:read",
            "workflows:run",
            "schedules:read",
            "config:read",
        ]

        self.assertTrue(
            has_required_scopes(
                UAT_SMOKE_SERVICE_ACCOUNT_SCOPES,
                required_scopes,
            )
        )
        self.assertNotIn("agent_os:admin", UAT_SMOKE_SERVICE_ACCOUNT_SCOPES)

    def test_dgx_owns_agentos_dependencies_and_uses_public_data_api(self) -> None:
        compose = (REPOSITORY_ROOT / "infra/uat/docker-compose.yaml").read_text()
        example_env = (REPOSITORY_ROOT / "infra/uat/.env.example").read_text()
        preflight = (REPOSITORY_ROOT / "infra/uat/preflight.sh").read_text()
        deploy = (REPOSITORY_ROOT / "infra/uat/deploy.sh").read_text()
        workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-uat.yml").read_text()
        migration = (REPOSITORY_ROOT / ".github/workflows/migrate-uat-state.yml").read_text()

        self.assertIn('"127.0.0.1:9081:9081"', compose)
        self.assertIn("postgres:", compose)
        self.assertIn("neo4j:", compose)
        self.assertIn("tidewise-agentos-uat-postgres-data", compose)
        self.assertIn("tidewise-agentos-uat-neo4j-data", compose)
        self.assertNotIn("ports:", compose.split("agentos:", 1)[0])
        self.assertIn(
            "AGENTOS_EXTERNAL_URL=https://tideai.tripwise.cn/agentos",
            example_env,
        )
        self.assertIn("DATA_SERVICE_BASE_URL=https://tideai.tripwise.cn", example_env)
        self.assertIn("public Data Service Source Snapshot", preflight)
        self.assertIn("is_private", preflight)
        self.assertNotIn("--insecure", preflight)
        self.assertNotIn("--insecure", deploy)
        self.assertNotIn("MINIO_", compose)
        self.assertNotIn("MINIO_", preflight)
        self.assertNotIn("MINIO_", workflow)
        self.assertNotIn("RDS_HOST", workflow)
        self.assertIn("DATA_SERVICE_BASE_URL: ${{ vars.DATA_SERVICE_BASE_URL }}", workflow)
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
        self.assertIn("pg_dump", deploy)
        self.assertIn("linux/arm64", workflow)
        self.assertIn("tidewise-agentos-uat-dgx", workflow)
        self.assertIn("MIGRATE_AGENTOS_UAT_TO_DGX", migration)
        self.assertIn("UAT_MIGRATION_PASSPHRASE", migration)
        self.assertIn("--no-owner --no-acl", migration)
        self.assertIn("migration-complete.sha256", migration)
        self.assertIn("refusing destructive restore", migration)
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
