"""Eastmoney public stock-news search tool."""

import html
import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from agno.run import RunContext

from capabilities.raw_collection.models import Candidate
from capabilities.raw_collection.tools.common import (
    ToolRequest,
    connector_endpoint,
    get_text,
    persist_candidates_async,
)

CONNECTOR = "eastmoney_stock_news"
DEFAULT_ENDPOINT = "https://search-api-web.eastmoney.com/search/jsonp"
_HTML_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"</?(?:br|div|h[1-6]|li|p|tr)\b[^>]*>", re.IGNORECASE)


def _plain_text(value: str) -> str:
    with_block_spacing = _BLOCK_TAG.sub(" ", value)
    return " ".join(html.unescape(_HTML_TAG.sub("", with_block_spacing)).split())


def _parse_china_datetime(value: str) -> datetime | None:
    try:
        local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return None
    return local.astimezone(UTC)


def _parse_requested_datetime(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed.astimezone(UTC)


def _requested_window(published_after: str, published_before: str) -> tuple[datetime, datetime]:
    after = _parse_requested_datetime(published_after, "published_after")
    before = _parse_requested_datetime(published_before, "published_before")
    if after >= before:
        raise ValueError("published_after must be earlier than published_before")
    maximum_hours = int(os.getenv("COLLECTOR_MAX_QUERY_WINDOW_HOURS", "8760"))
    if before - after > timedelta(hours=maximum_hours):
        raise ValueError("requested time window is too large")
    if before > datetime.now(UTC) + timedelta(minutes=15):
        raise ValueError("published_before is implausibly far in the future")
    return after, before


def _collection_provenance(run_context: RunContext) -> tuple[str, int, str]:
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
    return component_id, version, instructions_sha256


def _decode_jsonp(payload: str) -> dict[str, Any]:
    prefix = "callback("
    value = payload.strip()
    if not value.startswith(prefix) or not value.endswith(")"):
        raise ValueError("Eastmoney returned an invalid JSONP envelope")
    decoded = json.loads(value[len(prefix) : -1])
    if not isinstance(decoded, dict) or decoded.get("code") != 0:
        raise ValueError("Eastmoney returned an unsuccessful response")
    return decoded


async def search_eastmoney_stock_news(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Search recent Eastmoney stock news using a concise Chinese keyword.

    Use this tool when the objective involves A-share sectors, listed companies,
    policies, orders, prices, supply and demand, capacity, or technology events.
    The full direct results are persisted for deterministic Artifact construction;
    the returned JSON is only a small receipt for collection planning.

    Args:
        keyword: A concise Chinese search phrase, not an instruction sentence.
        published_after: Inclusive ISO 8601 lower bound with timezone, calculated from the user's prompt.
        published_before: Inclusive ISO 8601 upper bound with timezone, calculated from the user's prompt.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    keyword = keyword.strip()
    if not keyword or len(keyword) > 128:
        return json.dumps({"error": "keyword must contain 1..128 characters"}, ensure_ascii=False)
    if limit < 1 or limit > 10:
        return json.dumps({"error": "limit must be between 1 and 10"}, ensure_ascii=False)
    try:
        requested_after, requested_before = _requested_window(published_after, published_before)
        _collection_provenance(run_context)
    except (TypeError, ValueError):
        return json.dumps({"error": "collection time window or provenance is invalid"}, ensure_ascii=False)

    search_parameter = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "time",
                "pageIndex": 1,
                "pageSize": limit,
            }
        },
    }
    try:
        endpoint = connector_endpoint("EASTMONEY_STOCK_NEWS_BASE_URL", DEFAULT_ENDPOINT)
        payload = await get_text(
            endpoint,
            {
                "cb": "callback",
                "param": json.dumps(search_parameter, ensure_ascii=False, separators=(",", ":")),
                "_": "0",
            },
            {"Referer": "https://so.eastmoney.com/", "User-Agent": "Tidewise-AgentOS/1.0"},
            max_bytes=2_000_000,
        )
        decoded = _decode_jsonp(payload)
    except Exception:
        return json.dumps({"error": "Eastmoney stock-news search failed safely"}, ensure_ascii=False)

    rows = decoded.get("result", {}).get("cmsArticleWebOld", [])
    if not isinstance(rows, list):
        rows = []
    collected_at = datetime.now(UTC)
    candidates: list[Candidate] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _plain_text(str(row.get("title", "")))
        content = _plain_text(str(row.get("content", ""))) or title
        url = str(row.get("url", "")).strip()
        if not title or not url:
            continue
        try:
            candidate = Candidate(
                candidate_id=str(uuid4()),
                connector=CONNECTOR,
                query=keyword,
                title=title,
                url=url,
                content=content,
                source_name=_plain_text(str(row.get("mediaName", ""))) or "东方财富",
                source_external_id=str(row.get("code", "")).strip() or None,
                published_at=_parse_china_datetime(str(row.get("date", ""))),
                collected_at=collected_at,
            )
        except ValueError:
            continue
        candidates.append(candidate)

    request = ToolRequest(
        query=keyword,
        published_after=requested_after,
        published_before=requested_before,
        limit=limit,
    )
    return await persist_candidates_async(CONNECTOR, request, run_context, candidates)
