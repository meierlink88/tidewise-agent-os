"""Bounded Data Service HTTP client for Evidence publication."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def post_publication(endpoint: str, payload: dict[str, Any]) -> None:
    """POST one strict publication request within the three-second client budget."""
    base_url = os.getenv("DATA_SERVICE_BASE_URL", "http://data:9011").rstrip("/")
    token = os.getenv("DATA_SERVICE_TOKEN", "").strip()
    if not token:
        raise ValueError("DATA_SERVICE_TOKEN is not configured")
    request = Request(
        f"{base_url}/api/data/v1/{endpoint}",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            body = json.loads(response.read())
            if response.status != 201 or not isinstance(body, dict):
                raise ValueError(f"Data Service returned unexpected status {response.status}")
    except HTTPError as exc:
        try:
            error = json.loads(exc.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            error = {}
        code = error.get("error", {}).get("code") if isinstance(error, dict) else None
        raise ValueError(f"Data Service publication failed with HTTP {exc.code}: {code or 'UNKNOWN'}") from exc
    except (TimeoutError, URLError) as exc:
        raise ValueError("Data Service publication exceeded its three-second availability budget") from exc
