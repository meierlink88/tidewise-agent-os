"""Shared safety, HTTP, time-window, and persistence helpers for collection tools."""

import asyncio
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from agno.run import RunContext

from capabilities.raw_collection.buffer import write_tool_batch
from capabilities.raw_collection.models import Candidate, ToolBatchReceipt

DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT_SECONDS = 30
_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(?:br|div|h[1-6]|li|p|tr)\b[^>]*>", re.IGNORECASE)


class ToolConfigurationError(ValueError):
    """Raised when an operator-managed connector setting is absent."""


@dataclass(frozen=True)
class ToolRequest:
    """Validated arguments shared by every collection channel."""

    query: str
    published_after: datetime
    published_before: datetime
    limit: int


def plain_text(value: Any) -> str:
    """Return readable direct provider text without rewriting its meaning."""
    text = "" if value is None else str(value)
    with_block_spacing = _BLOCK_TAG.sub(" ", text)
    return " ".join(html.unescape(_HTML_TAG.sub("", with_block_spacing)).split())


def source_host(url: str) -> str:
    return urlsplit(url.strip()).hostname or ""


def parse_provider_datetime(value: Any, *, naive_timezone: tzinfo = UTC) -> datetime | None:
    """Parse common provider timestamps and normalize them to UTC."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        if number > 10_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(UTC)


def parse_china_datetime(value: Any) -> datetime | None:
    return parse_provider_datetime(value, naive_timezone=ZoneInfo("Asia/Shanghai"))


def _parse_requested_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(UTC)


def prepare_tool_request(
    query: str,
    published_after: str,
    published_before: str,
    limit: int,
) -> ToolRequest:
    query = query.strip()
    if not query or len(query) > 512:
        raise ValueError("query must contain 1..512 characters")
    if limit < 1 or limit > DEFAULT_LIMIT:
        raise ValueError(f"limit must be between 1 and {DEFAULT_LIMIT}")
    after = _parse_requested_datetime(published_after, "published_after")
    before = _parse_requested_datetime(published_before, "published_before")
    if after >= before:
        raise ValueError("published_after must be earlier than published_before")
    maximum_hours = int(os.getenv("COLLECTOR_MAX_QUERY_WINDOW_HOURS", "8760"))
    if before - after > timedelta(hours=maximum_hours):
        raise ValueError("requested time window is too large")
    if before > datetime.now(UTC) + timedelta(minutes=15):
        raise ValueError("published_before is implausibly far in the future")
    return ToolRequest(query=query, published_after=after, published_before=before, limit=limit)


def require_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ToolConfigurationError(f"{name} is not configured")
    return value


def connector_endpoint(name: str, default: str) -> str:
    endpoint = os.getenv(name, default).strip()
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ToolConfigurationError(f"{name} is invalid")
    return endpoint


def _connector_timeout() -> int:
    timeout = int(os.getenv("COLLECTOR_CONNECTOR_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    if timeout < 1 or timeout > 300:
        raise ToolConfigurationError("COLLECTOR_CONNECTOR_TIMEOUT_SECONDS is invalid")
    return timeout


async def get_text(
    endpoint: str,
    params: dict[str, str],
    headers: dict[str, str] | None = None,
    *,
    max_bytes: int = 20_000_000,
) -> str:
    """Read a bounded provider response without blocking the AgentOS event loop."""
    async with httpx.AsyncClient(timeout=_connector_timeout(), follow_redirects=True) as client:
        response = await client.get(
            endpoint,
            params=params,
            headers={
                "Accept": "application/json,*/*",
                "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                **(headers or {}),
            },
        )
        response.raise_for_status()
    if len(response.content) > max_bytes:
        raise ValueError("provider response is too large")
    return response.content.decode("utf-8", "replace")


def _decode_json_payload(payload: str) -> dict[str, Any]:
    value = payload.strip()
    if not value.startswith("{"):
        start = value.find("(")
        end = value.rfind(")")
        if start < 0 or end <= start:
            raise ValueError("provider response must be JSON or JSONP")
        value = value[start + 1 : end]
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("provider response must be a JSON object")
    return decoded


async def get_json(
    endpoint: str,
    params: dict[str, str],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _decode_json_payload(await get_text(endpoint, params, headers))


async def post_json(endpoint: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """POST JSON to a provider without blocking the AgentOS event loop."""
    async with httpx.AsyncClient(timeout=_connector_timeout(), follow_redirects=True) as client:
        response = await client.post(
            endpoint,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", **headers},
        )
        response.raise_for_status()
    if len(response.content) > 20_000_000:
        raise ValueError("provider response is too large")
    return _decode_json_payload(response.content.decode("utf-8", "replace"))


def persist_candidates(
    connector: str,
    request: ToolRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    dependencies = run_context.dependencies or {}
    component_id = dependencies.get("collector_agent_component_id")
    version = dependencies.get("collector_agent_config_version")
    instructions_sha256 = dependencies.get("collector_instructions_sha256")
    if not isinstance(component_id, str) or not component_id:
        raise ValueError("collector Agent component identity is missing")
    if not isinstance(version, int) or version < 1:
        raise ValueError("collector Agent config version is missing")
    if not isinstance(instructions_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", instructions_sha256):
        raise ValueError("collector Agent instructions hash is missing")

    batch = write_tool_batch(
        collection_id=run_context.run_id,
        connector=connector,
        query=request.query,
        requested_after=request.published_after,
        requested_before=request.published_before,
        agent_component_id=component_id,
        agent_config_version=version,
        instructions_sha256=instructions_sha256,
        candidates=candidates,
    )
    in_window = sum(
        request.published_after <= (item.published_at or item.collected_at) <= request.published_before
        for item in batch.candidates
    )
    return ToolBatchReceipt(
        batch_id=batch.batch_id,
        connector=batch.connector,
        query=batch.query,
        requested_after=batch.requested_after,
        requested_before=batch.requested_before,
        result_count=len(batch.candidates),
        in_window_result_count=in_window,
        candidate_ids=[item.candidate_id for item in batch.candidates],
    ).model_dump_json(exclude_none=True)


async def persist_candidates_async(
    connector: str,
    request: ToolRequest,
    run_context: RunContext,
    candidates: list[Candidate],
) -> str:
    """Persist a Tool Batch on a worker thread so file I/O cannot block AgentOS."""
    return await asyncio.to_thread(persist_candidates, connector, request, run_context, candidates)


def safe_tool_error(connector: str, code: str) -> str:
    """Return a stable model-visible error without provider or secret details."""
    return json.dumps({"connector": connector, "error": code}, ensure_ascii=False)
