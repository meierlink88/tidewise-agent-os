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

    def test_agentos_is_loopback_only_behind_shared_https_ingress(self) -> None:
        compose = (REPOSITORY_ROOT / "infra/uat/docker-compose.yaml").read_text()
        example_env = (REPOSITORY_ROOT / "infra/uat/.env.example").read_text()
        nginx = (REPOSITORY_ROOT / "infra/uat/nginx-agentos-location.conf").read_text()
        preflight = (REPOSITORY_ROOT / "infra/uat/preflight.sh").read_text()
        deploy = (REPOSITORY_ROOT / "infra/uat/deploy.sh").read_text()

        self.assertIn('"127.0.0.1:9081:9081"', compose)
        self.assertIn(
            "AGENTOS_EXTERNAL_URL=https://tideai.tripwise.cn/agentos",
            example_env,
        )
        self.assertIn("/.well-known/oauth-authorization-server/agentos", nginx)
        self.assertIn("/.well-known/oauth-protected-resource/agentos/mcp", nginx)
        self.assertIn('--resolve "${external_hostname}:443:127.0.0.1"', preflight)
        self.assertIn('--resolve "${external_hostname}:443:127.0.0.1"', deploy)
        self.assertNotIn("--insecure", preflight)
        self.assertNotIn("--insecure", deploy)

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
