"""Tavily recent-news direct-result collection tool."""

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

CONNECTOR = "tavily"
DEFAULT_ENDPOINT = "https://api.tavily.com/search"


async def search_tavily_news(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Search Tavily's news index and persist direct raw Markdown when available.

    Args:
        keyword: Focused natural-language news query.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(keyword, published_after, published_before, limit)
        body: dict[str, object] = {
            "query": tool_request.query,
            "topic": "news",
            "search_depth": "advanced",
            "auto_parameters": False,
            "chunks_per_source": 3,
            "max_results": tool_request.limit,
            "include_answer": False,
            "include_raw_content": "markdown",
        }
        if tool_request.published_before - tool_request.published_after <= timedelta(days=1):
            body["time_range"] = "day"
        else:
            body["start_date"] = tool_request.published_after.date().isoformat()
            body["end_date"] = tool_request.published_before.date().isoformat()
        payload = await post_json(
            connector_endpoint("TAVILY_SEARCH_BASE_URL", DEFAULT_ENDPOINT),
            body,
            {"Authorization": f"Bearer {require_secret('TAVILY_API_KEY')}"},
        )
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("Tavily results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            title = str(row.get("title", "")).strip()
            content = str(row.get("raw_content", "")).strip() or str(row.get("content", "")).strip()
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200] or url,
                        url=url,
                        content=content or title,
                        source_name=source_host(url) or "Tavily",
                        published_at=parse_provider_datetime(row.get("published_date")),
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
