from pathlib import Path
from unittest import TestCase

from agno.os import AgentOSBuiltinAuth
from fastmcp import FastMCP

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class UatIngressContractTest(TestCase):
    def test_agentos_is_loopback_only_behind_shared_https_ingress(self) -> None:
        compose = (REPOSITORY_ROOT / "infra/uat/docker-compose.yaml").read_text()
        example_env = (REPOSITORY_ROOT / "infra/uat/.env.example").read_text()
        nginx = (REPOSITORY_ROOT / "infra/uat/nginx-agentos-location.conf").read_text()

        self.assertIn('"127.0.0.1:9081:9081"', compose)
        self.assertIn(
            "AGENTOS_EXTERNAL_URL=https://tideai.tripwise.cn/agentos",
            example_env,
        )
        self.assertIn("/.well-known/oauth-authorization-server/agentos", nginx)
        self.assertIn("/.well-known/oauth-protected-resource/agentos/mcp", nginx)

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
