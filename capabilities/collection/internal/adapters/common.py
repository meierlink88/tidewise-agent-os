"""HTTP, parsing, and provenance helpers shared by provider Adapters."""

import html
import json
import re
from datetime import UTC, datetime, tzinfo
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from capabilities.collection.internal.channels.models import CollectionChannel
from capabilities.collection.internal.models import SourceLevel

_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(?:br|div|h[1-6]|li|p|tr)\b[^>]*>", re.IGNORECASE)


def plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    with_spacing = _BLOCK_TAG.sub(" ", text)
    return " ".join(html.unescape(_HTML_TAG.sub("", with_spacing)).split())


def source_host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def parse_provider_datetime(value: Any, *, naive_timezone: tzinfo = UTC) -> datetime | None:
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


def web_source_level(channel: CollectionChannel, url: str) -> SourceLevel:
    mappings = channel.config.get("source_levels", {})
    host = source_host(url)
    if isinstance(mappings, dict):
        for configured_host, value in mappings.items():
            domain = str(configured_host).lower().strip().lstrip(".")
            if domain and (host == domain or host.endswith(f".{domain}")):
                try:
                    return SourceLevel(str(value))
                except ValueError:
                    continue
    return channel.default_source_level


async def get_text(
    endpoint: str,
    params: dict[str, str],
    headers: dict[str, str] | None = None,
    *,
    timeout_seconds: int,
    max_bytes: int = 20_000_000,
) -> str:
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(
            endpoint,
            params=params,
            headers={
                "Accept": "application/json,application/rss+xml,application/atom+xml,*/*",
                "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                **(headers or {}),
            },
        )
        response.raise_for_status()
    if len(response.content) > max_bytes:
        raise ValueError("provider response is too large")
    return response.content.decode("utf-8", "replace")


def decode_json_payload(payload: str) -> dict[str, Any]:
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
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    return decode_json_payload(await get_text(endpoint, params, headers, timeout_seconds=timeout_seconds))


async def post_json(
    endpoint: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.post(
            endpoint,
            content=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", **headers},
        )
        response.raise_for_status()
    if len(response.content) > 20_000_000:
        raise ValueError("provider response is too large")
    return decode_json_payload(response.content.decode("utf-8", "replace"))
