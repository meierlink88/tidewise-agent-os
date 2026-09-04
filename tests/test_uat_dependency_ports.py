import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "infra/uat/verify-dependency-ports.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_uat_dependency_ports", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load UAT dependency port verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UatDependencyPortVerifierTest(TestCase):
    def setUp(self) -> None:
        self.module = _load_script()
        self.argv = [
            str(SCRIPT_PATH),
            "postgres-container",
            "neo4j-container",
            "minio-container",
            "192.168.0.53",
            "15432",
            "7474",
            "7687",
            "9000",
            "9001",
        ]

    def test_accepts_only_the_exact_private_lan_bindings(self) -> None:
        postgres_ports = {"5432/tcp": [{"HostIp": "192.168.0.53", "HostPort": "15432"}]}
        neo4j_ports = {
            "7473/tcp": None,
            "7474/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7474"}],
            "7687/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7687"}],
        }
        minio_ports = {
            "9000/tcp": [{"HostIp": "192.168.0.53", "HostPort": "9000"}],
            "9001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9001"}],
        }

        with (
            patch.object(self.module.sys, "argv", self.argv),
            patch.object(
                self.module.subprocess,
                "check_output",
                side_effect=[json.dumps(postgres_ports), json.dumps(neo4j_ports), json.dumps(minio_ports)],
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(self.module.main(), 0)

        self.assertIn("PASS protected-lan-dependency-bindings", output.getvalue())

    def test_rejects_an_all_interfaces_binding(self) -> None:
        postgres_ports = {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "15432"}]}
        neo4j_ports = {
            "7474/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7474"}],
            "7687/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7687"}],
        }
        minio_ports = {
            "9000/tcp": [{"HostIp": "192.168.0.53", "HostPort": "9000"}],
            "9001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9001"}],
        }

        with (
            patch.object(self.module.sys, "argv", self.argv),
            patch.object(
                self.module.subprocess,
                "check_output",
                side_effect=[json.dumps(postgres_ports), json.dumps(neo4j_ports), json.dumps(minio_ports)],
            ),
            self.assertRaisesRegex(SystemExit, "PostgreSQL bindings"),
        ):
            self.module.main()

    def test_rejects_a_public_bind_address_before_inspection(self) -> None:
        invalid_argv = [*self.argv]
        invalid_argv[4] = "8.8.8.8"

        with (
            patch.object(self.module.sys, "argv", invalid_argv),
            patch.object(self.module.subprocess, "check_output") as inspect,
            self.assertRaisesRegex(SystemExit, "private IPv4"),
        ):
            self.module.main()

        inspect.assert_not_called()

    def test_rejects_public_minio_console_binding(self) -> None:
        postgres_ports = {"5432/tcp": [{"HostIp": "192.168.0.53", "HostPort": "15432"}]}
        neo4j_ports = {
            "7474/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7474"}],
            "7687/tcp": [{"HostIp": "192.168.0.53", "HostPort": "7687"}],
        }
        minio_ports = {
            "9000/tcp": [{"HostIp": "192.168.0.53", "HostPort": "9000"}],
            "9001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9001"}],
        }

        with (
            patch.object(self.module.sys, "argv", self.argv),
            patch.object(
                self.module.subprocess,
                "check_output",
                side_effect=[json.dumps(postgres_ports), json.dumps(neo4j_ports), json.dumps(minio_ports)],
            ),
            self.assertRaisesRegex(SystemExit, "MinIO bindings"),
        ):
            self.module.main()
