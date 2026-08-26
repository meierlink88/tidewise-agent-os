"""Authenticated Reasoning Server Event Candidate client."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def post_event_candidate(payload: dict[str, object]) -> dict[str, object]:
    base_url = os.getenv("REASON_SERVICE_BASE_URL", "http://reason-graphiti-api:8890").rstrip("/")
    token = os.getenv("REASON_SERVICE_TOKEN", "")
    if not token:
        raise ValueError("REASON_SERVICE_TOKEN is required")
    request = Request(
        f"{base_url}/api/reason/v1/event-candidates",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        raise ValueError(f"Reasoning Server rejected Event Candidate with HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("Reasoning Server request failed") from exc
    if status != 202:
        raise ValueError(f"Reasoning Server returned unexpected HTTP {status}")
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Reasoning Server acceptance response is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Reasoning Server acceptance response is invalid")
    return decoded
