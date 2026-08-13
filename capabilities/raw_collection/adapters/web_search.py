"""Web Search provider Adapters."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from capabilities.raw_collection.adapters.base import FetchRequest
from capabilities.raw_collection.adapters.common import (
    parse_provider_datetime,
    post_json,
    source_host,
    web_source_level,
)
from capabilities.raw_collection.channels.models import CollectionChannel
from capabilities.raw_collection.models import Candidate


def _key(channel: CollectionChannel) -> str:
    value = (channel.app_key or "").strip()
    if not value:
        raise ValueError("channel app_key is not configured")
    return value


def _candidate(
    channel: CollectionChannel,
    request: FetchRequest,
    *,
    title: str,
    url: str,
    content: str,
    source_name: str,
    published_at: datetime | None,
    collected_at: datetime,
) -> Candidate | None:
    try:
        return Candidate(
            candidate_id=str(uuid4()),
            connector=channel.code,
            query=request.query,
            title=title or content[:200] or url,
            url=url,
            content=content or title,
            source_name=source_name or source_host(url) or channel.name,
            source_level=web_source_level(channel, url),
            published_at=published_at,
            collected_at=collected_at,
        )
    except ValueError:
        return None


class BochaAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        window = request.published_before - request.published_after
        freshness = (
            "oneDay" if window <= timedelta(days=1) else "oneWeek" if window <= timedelta(days=7) else "oneMonth"
        )
        payload = await post_json(
            str(channel.endpoint),
            {"query": request.query, "freshness": freshness, "summary": True, "count": channel.max_results},
            {"Authorization": f"Bearer {_key(channel)}"},
            timeout_seconds=channel.timeout_seconds,
        )
        data = payload.get("data", {})
        web_pages = data.get("webPages", {}) if isinstance(data, dict) else {}
        rows = web_pages.get("value", []) if isinstance(web_pages, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Bocha results must be a list")
        collected_at = datetime.now(UTC)
        candidates = [
            _candidate(
                channel,
                request,
                title=str(row.get("name", "")).strip(),
                url=str(row.get("url", "")).strip(),
                content=str(row.get("summary", "")).strip() or str(row.get("snippet", "")).strip(),
                source_name=str(row.get("siteName", "")).strip(),
                published_at=parse_provider_datetime(row.get("datePublished")),
                collected_at=collected_at,
            )
            for row in rows[: channel.max_results]
            if isinstance(row, dict)
        ]
        return [item for item in candidates if item is not None]


class TavilyAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        body: dict[str, Any] = {
            "query": request.query,
            "topic": "news",
            "search_depth": "advanced",
            "auto_parameters": False,
            "chunks_per_source": 3,
            "max_results": channel.max_results,
            "include_answer": False,
            "include_raw_content": "markdown",
        }
        if request.published_before - request.published_after <= timedelta(days=1):
            body["time_range"] = "day"
        else:
            body["start_date"] = request.published_after.date().isoformat()
            body["end_date"] = request.published_before.date().isoformat()
        payload = await post_json(
            str(channel.endpoint),
            body,
            {"Authorization": f"Bearer {_key(channel)}"},
            timeout_seconds=channel.timeout_seconds,
        )
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("Tavily results must be a list")
        collected_at = datetime.now(UTC)
        candidates = [
            _candidate(
                channel,
                request,
                title=str(row.get("title", "")).strip(),
                url=str(row.get("url", "")).strip(),
                content=str(row.get("raw_content", "")).strip() or str(row.get("content", "")).strip(),
                source_name=source_host(str(row.get("url", ""))),
                published_at=parse_provider_datetime(row.get("published_date")),
                collected_at=collected_at,
            )
            for row in rows[: channel.max_results]
            if isinstance(row, dict)
        ]
        return [item for item in candidates if item is not None]


class ParallelAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        payload = await post_json(
            str(channel.endpoint),
            {"objective": request.query, "search_queries": [request.query], "max_chars_total": 50_000},
            {"x-api-key": _key(channel)},
            timeout_seconds=channel.timeout_seconds,
        )
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("Parallel results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate | None] = []
        for row in rows[: channel.max_results]:
            if not isinstance(row, dict):
                continue
            excerpts = row.get("excerpts", [])
            content = (
                "\n\n".join(str(item).strip() for item in excerpts if str(item).strip())
                if isinstance(excerpts, list)
                else ""
            )
            candidates.append(
                _candidate(
                    channel,
                    request,
                    title=str(row.get("title", "")).strip(),
                    url=str(row.get("url", "")).strip(),
                    content=content,
                    source_name=source_host(str(row.get("url", ""))),
                    published_at=parse_provider_datetime(row.get("publish_date")),
                    collected_at=collected_at,
                )
            )
        return [item for item in candidates if item is not None]
