#!/usr/bin/env python3

from __future__ import annotations

import sys
from urllib.parse import urlparse


def loopback_resolve_entry(public_url: str) -> str:
    parsed = urlparse(public_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(str(error)) from error
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("absolute HTTPS URL required")
    if port not in {None, 443}:
        raise ValueError("port 443 is required")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("credentials, query, and fragment are not allowed")
    return f"{parsed.hostname}:443:127.0.0.1"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: resolve_loopback_https.py PUBLIC_URL")
    try:
        print(loopback_resolve_entry(sys.argv[1]))
    except ValueError as error:
        raise SystemExit(f"FAIL raw-evidence-public-url: {error}") from error


if __name__ == "__main__":
    main()
