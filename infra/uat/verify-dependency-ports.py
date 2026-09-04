#!/usr/bin/env python3
"""Verify the exact host bindings for UAT-owned infrastructure."""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys


def _published_bindings(container_id: str) -> dict[str, list[tuple[str, int]]]:
    raw = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", container_id],
            text=True,
        )
    )
    return {
        container_port: sorted((binding["HostIp"], int(binding["HostPort"])) for binding in bindings)
        for container_port, bindings in raw.items()
        if bindings
    }


def _port(value: str, name: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise SystemExit(f"FAIL dependency-ports: {name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"FAIL dependency-ports: {name} is outside 1..65535")
    return port


def main() -> int:
    if len(sys.argv) != 10:
        raise SystemExit(
            "usage: verify-dependency-ports.py POSTGRES_CONTAINER NEO4J_CONTAINER "
            "MINIO_CONTAINER LAN_ADDRESS POSTGRES_PORT NEO4J_HTTP_PORT "
            "NEO4J_BOLT_PORT MINIO_API_PORT MINIO_CONSOLE_PORT"
        )

    (
        postgres_container,
        neo4j_container,
        minio_container,
        address_text,
        postgres_text,
        http_text,
        bolt_text,
        minio_api_text,
        minio_console_text,
    ) = sys.argv[1:]
    address = ipaddress.ip_address(address_text)
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified:
        raise SystemExit("FAIL dependency-ports: LAN address must be a specific private IPv4 address")

    postgres_port = _port(postgres_text, "POSTGRES_LAN_PORT")
    http_port = _port(http_text, "NEO4J_HTTP_LAN_PORT")
    bolt_port = _port(bolt_text, "NEO4J_BOLT_LAN_PORT")
    minio_api_port = _port(minio_api_text, "MINIO_LAN_PORT")
    minio_console_port = _port(minio_console_text, "MINIO_CONSOLE_PORT")
    if len({postgres_port, http_port, bolt_port, minio_api_port, minio_console_port}) != 5:
        raise SystemExit("FAIL dependency-ports: published ports must be unique")

    expected_postgres = {"5432/tcp": [(address_text, postgres_port)]}
    expected_neo4j = {
        "7474/tcp": [(address_text, http_port)],
        "7687/tcp": [(address_text, bolt_port)],
    }
    expected_minio = {
        "9000/tcp": [(address_text, minio_api_port)],
        "9001/tcp": [("127.0.0.1", minio_console_port)],
    }
    actual_postgres = _published_bindings(postgres_container)
    actual_neo4j = _published_bindings(neo4j_container)
    actual_minio = _published_bindings(minio_container)
    if actual_postgres != expected_postgres:
        raise SystemExit(
            f"FAIL dependency-ports: PostgreSQL bindings {actual_postgres!r} do not match {expected_postgres!r}"
        )
    if actual_neo4j != expected_neo4j:
        raise SystemExit(f"FAIL dependency-ports: Neo4j bindings {actual_neo4j!r} do not match {expected_neo4j!r}")
    if actual_minio != expected_minio:
        raise SystemExit(f"FAIL dependency-ports: MinIO bindings {actual_minio!r} do not match {expected_minio!r}")

    print(
        "PASS protected-lan-dependency-bindings "
        f"postgres={address_text}:{postgres_port} "
        f"neo4j-http={address_text}:{http_port} neo4j-bolt={address_text}:{bolt_port} "
        f"minio-api={address_text}:{minio_api_port} minio-console=127.0.0.1:{minio_console_port}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
