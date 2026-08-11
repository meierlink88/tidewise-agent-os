"""Public professional-news channel tools used by the Raw Collector Agent."""

import hashlib
from datetime import UTC, datetime
from urllib.parse import urljoin
from uuid import uuid4

from agno.run import RunContext

from capabilities.raw_collection.models import Candidate
from capabilities.raw_collection.tools.common import (
    ToolConfigurationError,
    connector_endpoint,
    get_json,
    parse_china_datetime,
    parse_provider_datetime,
    persist_candidates_async,
    plain_text,
    prepare_tool_request,
    safe_tool_error,
)

CLS_CONNECTOR = "cls_telegraph"
EASTMONEY_FAST_CONNECTOR = "eastmoney_fastnews"
STCN_CONNECTOR = "stcn_quicknews"

CLS_DEFAULT_ENDPOINT = "https://www.cls.cn/v1/roll/get_roll_list"
EASTMONEY_FAST_DEFAULT_ENDPOINT = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
STCN_DEFAULT_ENDPOINT = "https://www.stcn.com/article/list.html"


def _first_non_empty(*values: object) -> str:
    for value in values:
        text = plain_text(value)
        if text:
            return text
    return ""


def _cls_signature(params: dict[str, str]) -> str:
    canonical = "&".join(f"{key}={params[key]}" for key in sorted(params))
    first = hashlib.sha1(canonical.encode()).hexdigest()  # noqa: S324 - provider signing contract.
    return hashlib.md5(first.encode()).hexdigest()  # noqa: S324 - provider signing contract.


async def search_cls_telegraph(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Collect the latest direct telegraph entries from 财联社.

    Args:
        keyword: Short description of the topic being collected; the feed itself is chronological.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(keyword, published_after, published_before, limit)
        params = {
            "appName": "CailianpressWeb",
            "os": "web",
            "sv": "7.7.5",
            "last_time": "",
            "refresh_type": "1",
            "rn": str(tool_request.limit),
        }
        payload = await get_json(
            connector_endpoint("CLS_TELEGRAPH_BASE_URL", CLS_DEFAULT_ENDPOINT),
            {**params, "sign": _cls_signature(params)},
            {"Referer": "https://www.cls.cn/"},
        )
        data = payload.get("data", {})
        rows = data.get("roll_data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise ValueError("CLS results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            external_id = plain_text(row.get("id"))
            url = f"https://www.cls.cn/detail/{external_id}" if external_id else ""
            title = _first_non_empty(row.get("title"), row.get("brief"))
            content = _first_non_empty(row.get("content"), row.get("brief"), title)
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=CLS_CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200],
                        url=url,
                        content=content,
                        source_name="财联社",
                        source_external_id=external_id or None,
                        published_at=parse_provider_datetime(row.get("ctime")),
                        collected_at=collected_at,
                    )
                )
            except ValueError:
                continue
        return await persist_candidates_async(CLS_CONNECTOR, tool_request, run_context, candidates)
    except ToolConfigurationError:
        return safe_tool_error(CLS_CONNECTOR, "not_configured")
    except ValueError:
        return safe_tool_error(CLS_CONNECTOR, "invalid_request_or_response")
    except Exception:
        return safe_tool_error(CLS_CONNECTOR, "request_failed")


async def search_eastmoney_fast_news(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Collect the latest direct 7x24 entries from 东方财富.

    Args:
        keyword: Short description of the topic being collected; the feed itself is chronological.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(keyword, published_after, published_before, limit)
        payload = await get_json(
            connector_endpoint("EASTMONEY_FAST_NEWS_BASE_URL", EASTMONEY_FAST_DEFAULT_ENDPOINT),
            {
                "client": "web",
                "biz": "web_724",
                "fastColumn": "102",
                "sortEnd": "",
                "pageSize": str(tool_request.limit),
                "req_trace": str(uuid4()),
            },
            {"Referer": "https://kuaixun.eastmoney.com/"},
        )
        data = payload.get("data", {})
        rows = data.get("fastNewsList", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Eastmoney fast-news results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("code", "")).strip()
            title = plain_text(row.get("title"))
            content = _first_non_empty(row.get("summary"), title)
            url = f"https://finance.eastmoney.com/a/{external_id}.html" if external_id else ""
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=EASTMONEY_FAST_CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200],
                        url=url,
                        content=content,
                        source_name="东方财富",
                        source_external_id=external_id or None,
                        published_at=parse_china_datetime(row.get("showTime")),
                        collected_at=collected_at,
                    )
                )
            except ValueError:
                continue
        return await persist_candidates_async(EASTMONEY_FAST_CONNECTOR, tool_request, run_context, candidates)
    except ToolConfigurationError:
        return safe_tool_error(EASTMONEY_FAST_CONNECTOR, "not_configured")
    except ValueError:
        return safe_tool_error(EASTMONEY_FAST_CONNECTOR, "invalid_request_or_response")
    except Exception:
        return safe_tool_error(EASTMONEY_FAST_CONNECTOR, "request_failed")


async def search_stcn_quick_news(
    keyword: str,
    published_after: str,
    published_before: str,
    run_context: RunContext,
    limit: int = 10,
) -> str:
    """Collect the latest direct quick-news entries from 证券时报.

    Args:
        keyword: Short description of the topic being collected; the feed itself is chronological.
        published_after: Inclusive ISO 8601 lower bound with timezone.
        published_before: Inclusive ISO 8601 upper bound with timezone.
        limit: Maximum direct results to retain, from 1 through 10.
    """
    try:
        tool_request = prepare_tool_request(keyword, published_after, published_before, limit)
        payload = await get_json(
            connector_endpoint("STCN_QUICK_NEWS_BASE_URL", STCN_DEFAULT_ENDPOINT),
            {"type": "kx"},
            {
                "Referer": "https://www.stcn.com/article/list/kx.html",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise ValueError("STCN quick-news results must be a list")
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for row in rows[: tool_request.limit]:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("id", "")).strip()
            relative_url = str(row.get("url", "")).strip()
            url = urljoin("https://www.stcn.com/", relative_url)
            title = plain_text(row.get("title"))
            content = _first_non_empty(row.get("content"), title)
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=STCN_CONNECTOR,
                        query=tool_request.query,
                        title=title or content[:200],
                        url=url,
                        content=content,
                        source_name=_first_non_empty(row.get("source"), "证券时报"),
                        source_external_id=external_id or None,
                        published_at=parse_provider_datetime(row.get("time")),
                        collected_at=collected_at,
                    )
                )
            except ValueError:
                continue
        return await persist_candidates_async(STCN_CONNECTOR, tool_request, run_context, candidates)
    except ToolConfigurationError:
        return safe_tool_error(STCN_CONNECTOR, "not_configured")
    except ValueError:
        return safe_tool_error(STCN_CONNECTOR, "invalid_request_or_response")
    except Exception:
        return safe_tool_error(STCN_CONNECTOR, "request_failed")
