import importlib.util
from pathlib import Path
from types import ModuleType
from unittest import TestCase

from agno.os import AgentOSBuiltinAuth
from agno.os.scopes import has_required_scopes
from fastmcp import FastMCP

from scripts.smoke_uat import UAT_SMOKE_SERVICE_ACCOUNT_SCOPES

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def load_loopback_resolver() -> ModuleType:
    path = REPOSITORY_ROOT / "infra/uat/resolve_loopback_https.py"
    spec = importlib.util.spec_from_file_location("resolve_loopback_https", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_agentos_is_loopback_only_behind_shared_https_ingress(self) -> None:
        compose = (REPOSITORY_ROOT / "infra/uat/docker-compose.yaml").read_text()
        example_env = (REPOSITORY_ROOT / "infra/uat/.env.example").read_text()
        nginx = (REPOSITORY_ROOT / "infra/uat/nginx-agentos-location.conf").read_text()
        preflight = (REPOSITORY_ROOT / "infra/uat/preflight.sh").read_text()
        deploy = (REPOSITORY_ROOT / "infra/uat/deploy.sh").read_text()
        workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-uat.yml").read_text()

        self.assertIn('"127.0.0.1:9081:9081"', compose)
        self.assertIn(
            "AGENTOS_EXTERNAL_URL=https://tideai.tripwise.cn/agentos",
            example_env,
        )
        self.assertIn("/.well-known/oauth-authorization-server/agentos", nginx)
        self.assertIn("/.well-known/oauth-protected-resource/agentos/mcp", nginx)
        self.assertIn('--resolve "${external_hostname}:443:127.0.0.1"', preflight)
        self.assertIn('python3 "${script_root}/resolve_loopback_https.py"', preflight)
        self.assertIn('--resolve "$public_resolve"', preflight)
        self.assertIn('--resolve "${external_hostname}:443:127.0.0.1"', deploy)
        self.assertNotIn("--insecure", preflight)
        self.assertNotIn("--insecure", deploy)
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
            "NEO4J_URI: ${NEO4J_URI:?NEO4J_URI is required}",
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
        self.assertIn("NEO4J_URI: ${{ vars.NEO4J_URI }}", workflow)
        self.assertIn("GRAPHITI_EMBEDDING_API_KEY: ${{ secrets.GRAPHITI_EMBEDDING_API_KEY }}", workflow)
        self.assertIn("EVENT_EXTRACTION_BATCH_SIZE: ${{ vars.EVENT_EXTRACTION_BATCH_SIZE || '20' }}", workflow)
        self.assertIn('"EVENT_EXTRACTION_BATCH_SIZE",', workflow)
        self.assertIn("driver.verify_connectivity()", preflight)
        self.assertIn("-m sematica.graphiti.readiness", preflight)
        self.assertIn("internal-neo4j-and-graphiti-embedding", preflight)
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


class UatLoopbackResolverTest(TestCase):
    def test_preserves_public_tls_hostname_while_targeting_loopback(self) -> None:
        resolver = load_loopback_resolver()

        self.assertEqual(
            resolver.loopback_resolve_entry("https://tideai.tripwise.cn/raw-evidence"),
            "tideai.tripwise.cn:443:127.0.0.1",
        )

    def test_rejects_urls_that_cannot_use_the_reviewed_tls_ingress(self) -> None:
        resolver = load_loopback_resolver()
        invalid_urls = (
            "http://tideai.tripwise.cn",
            "https://tideai.tripwise.cn:8443",
            "https://user:password@tideai.tripwise.cn",
            "https://tideai.tripwise.cn?download=true",
            "https://tideai.tripwise.cn#fragment",
            "/raw-evidence",
        )

        for invalid_url in invalid_urls:
            with self.subTest(url=invalid_url):
                with self.assertRaises(ValueError):
                    resolver.loopback_resolve_entry(invalid_url)
