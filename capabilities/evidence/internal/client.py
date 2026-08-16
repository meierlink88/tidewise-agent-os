"""Bounded Data Service HTTP client for Evidence publication."""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _data_service_settings() -> tuple[str, str]:
    base_url = os.getenv("DATA_SERVICE_BASE_URL", "http://data:9011").rstrip("/")
    token = os.getenv("DATA_SERVICE_TOKEN", "").strip()
    if not token:
        raise ValueError("DATA_SERVICE_TOKEN is not configured")
    return base_url, token


def _success_result(response: Any, *, expected_status: int) -> dict[str, Any]:
    body = json.loads(response.read())
    if response.status != expected_status:
        raise ValueError(f"Data Service returned unexpected status {response.status}")
    if (
        not isinstance(body, dict)
        or set(body) != {"request_id", "result"}
        or not isinstance(body.get("request_id"), str)
        or not body["request_id"].strip()
        or not isinstance(body.get("result"), dict)
    ):
        raise ValueError("Data Service returned an invalid success envelope")
    return body["result"]


def _http_error_code(exc: HTTPError) -> str:
    try:
        error = json.loads(exc.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        error = {}
    code = error.get("error", {}).get("code") if isinstance(error, dict) else None
    return code or "UNKNOWN"


def get_evidence_categories() -> dict[str, Any]:
    """GET the complete strict Evidence Category Catalog result."""
    base_url, token = _data_service_settings()
    request = Request(
        f"{base_url}/api/data/v1/evidence-categories",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            return _success_result(response, expected_status=200)
    except HTTPError as exc:
        raise ValueError(
            f"Data Service Evidence Category Catalog failed with HTTP {exc.code}: {_http_error_code(exc)}"
        ) from exc
    except (TimeoutError, URLError) as exc:
        raise ValueError(
            "Data Service Evidence Category Catalog exceeded its three-second availability budget"
        ) from exc


def post_publication(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST one strict publication request and return its result envelope member."""
    base_url, token = _data_service_settings()
    request = Request(
        f"{base_url}/api/data/v1/{endpoint}",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            return _success_result(response, expected_status=201)
    except HTTPError as exc:
        raise ValueError(f"Data Service publication failed with HTTP {exc.code}: {_http_error_code(exc)}") from exc
    except (TimeoutError, URLError) as exc:
        raise ValueError("Data Service publication exceeded its three-second availability budget") from exc
