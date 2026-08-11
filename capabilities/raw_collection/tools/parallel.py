"""Parallel Search direct-result collection tool."""

from datetime import UTC, datetime
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

CONNECTOR = "parallel_search"
DEFAULT_ENDPOINT = "https://api.parallel.ai/v1/search"


async def search_parallel_news(
    objective: str,
    search_queries: list[str],
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Search the public web through Parallel and persist its direct excerpts.

    Args:
        objective: Self-contained collection objective including topic and freshness intent.
        search_queries: One to five concise keyword queries, each no longer than 200 characters.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(objective, published_after, published_before, limit)
        queries = [item.strip() for item in search_queries if item.strip()]
        if not 1 <= len(queries) <= 5 or any(len(item) > 200 for item in queries):
            raise ValueError("search_queries must contain one to five concise queries")
        payload = await post_json(
            connector_endpoint("PARALLEL_SEARCH_BASE_URL", DEFAULT_ENDPOINT),
            {
                "objective": tool_request.query,
                "search_queries": queries,
                "max_chars_total": 50_000,
            },
            {"x-api-key": require_secret("PARALLEL_API_KEY")},
        )
        rows = payload.get("results", [])
        if not isinstance(rows, list):
            raise ValueError("Parallel results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url", "")).strip()
            title = str(row.get("title", "")).strip()
            excerpts = row.get("excerpts", [])
            content = (
                "\n\n".join(str(item).strip() for item in excerpts if str(item).strip())
                if isinstance(excerpts, list)
                else ""
            )
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200] or url,
                        url=url,
                        content=content or title,
                        source_name=source_host(url) or "Parallel Search",
                        published_at=parse_provider_datetime(row.get("publish_date")),
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
