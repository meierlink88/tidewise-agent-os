"""Bocha recent web-search direct-result collection tool."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agno.run import RunContext

from capabilities.raw_collection.models import Candidate
from capabilities.raw_collection.tools.common import (
    ToolConfigurationError,
    connector_endpoint,
    parse_provider_datetime,
    persist_candidates_async,
    post_json,
    prepare_tool_request,
    require_secret,
    safe_tool_error,
    source_host,
)

CONNECTOR = "bocha"
DEFAULT_ENDPOINT = "https://api.bochaai.com/v1/web-search"


def _freshness(window: timedelta) -> str:
    if window <= timedelta(days=1):
        return "oneDay"
    if window <= timedelta(days=7):
        return "oneWeek"
    if window <= timedelta(days=30):
        return "oneMonth"
    return "oneYear"


async def search_bocha_news(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Search Bocha and persist direct summaries without following result URLs.

    Args:
        keyword: Focused Chinese web-search query.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(keyword, published_after, published_before, limit)
        payload = await post_json(
            connector_endpoint("BOCHA_SEARCH_BASE_URL", DEFAULT_ENDPOINT),
            {
                "query": tool_request.query,
                "freshness": _freshness(tool_request.published_before - tool_request.published_after),
                "summary": True,
                "count": tool_request.limit,
            },
            {"Authorization": f"Bearer {require_secret('BOCHA_API_KEY')}"},
        )
        data = payload.get("data", {})
        web_pages = data.get("webPages", {}) if isinstance(data, dict) else {}
        rows = web_pages.get("value", []) if isinstance(web_pages, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Bocha results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            title = str(row.get("name", "")).strip()
            content = str(row.get("summary", "")).strip() or str(row.get("snippet", "")).strip()
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200] or url,
                        url=url,
                        content=content or title,
                        source_name=str(row.get("siteName", "")).strip() or source_host(url) or "博查",
                        published_at=parse_provider_datetime(row.get("datePublished")),
                        collected_at=collected_at,
                    )
                )
            except ValueError:
                continue
        return await persist_candidates_async(CONNECTOR, tool_request, run_context, candidates)
    except ToolConfigurationError:
        return safe_tool_error(CONNECTOR, "not_configured")
    except ValueError:
        return safe_tool_error(CONNECTOR, "invalid_request_or_response")
    except Exception:
        return safe_tool_error(CONNECTOR, "request_failed")
