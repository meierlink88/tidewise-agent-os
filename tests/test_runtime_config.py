"""Tests for deployment-facing runtime configuration contracts."""

import os
import unittest
from unittest.mock import patch

from db.url import build_db_url


class RuntimeConfigTest(unittest.TestCase):
    def test_database_url_supports_required_rds_tls(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DB_DRIVER": "postgresql+psycopg",
                "DB_USER": "agent_os_uat_runtime",
                "DB_PASS": "password with symbols/@",
                "DB_HOST": "rds.internal.example",
                "DB_PORT": "5432",
                "DB_DATABASE": "agent_os_uat",
                "DB_SSLMODE": "require",
            },
        ):
            url = build_db_url()

        self.assertEqual(
            url,
            "postgresql+psycopg://agent_os_uat_runtime:password%20with%20symbols%2F%40"
            "@rds.internal.example:5432/agent_os_uat?sslmode=require",
        )

    def test_local_database_url_does_not_force_tls(self) -> None:
        with patch.dict(os.environ, {"DB_SSLMODE": ""}, clear=False):
            self.assertNotIn("sslmode=", build_db_url())


if __name__ == "__main__":
    unittest.main()
