"""Structured professional-news API Adapters."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import urljoin
from uuid import uuid4

from capabilities.collection.internal.adapters.base import FetchRequest
from capabilities.collection.internal.adapters.common import (
    get_json,
    get_text,
    parse_china_datetime,
    parse_provider_datetime,
    plain_text,
)
from capabilities.collection.internal.channels.models import CollectionChannel
from capabilities.collection.internal.models import Candidate


def _first(*values: object) -> str:
    for value in values:
        text = plain_text(value)
        if text:
            return text
    return ""


def _candidate(
    channel: CollectionChannel,
    request: FetchRequest,
    *,
    title: str,
    url: str,
    content: str,
    source_name: str,
    source_external_id: str | None,
    published_at: datetime | None,
    collected_at: datetime,
) -> Candidate | None:
    try:
        return Candidate(
            candidate_id=str(uuid4()),
            connector=channel.code,
            query=request.query,
            title=title or content[:200],
            url=url,
            content=content,
            source_name=source_name or channel.name,
            source_level=channel.default_source_level,
            source_external_id=source_external_id,
            published_at=published_at,
            collected_at=collected_at,
        )
    except ValueError:
        return None


class ClsAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        params = {
            "appName": "CailianpressWeb",
            "os": "web",
            "sv": "7.7.5",
            "last_time": "",
            "refresh_type": "1",
            "rn": str(channel.max_results),
        }
        canonical = "&".join(f"{key}={params[key]}" for key in sorted(params))
        first = hashlib.sha1(canonical.encode()).hexdigest()  # noqa: S324 - provider signing contract.
        signature = hashlib.md5(first.encode()).hexdigest()  # noqa: S324 - provider signing contract.
        payload = await get_json(
            str(channel.endpoint),
            {**params, "sign": signature},
            {"Referer": "https://www.cls.cn/"},
            timeout_seconds=channel.timeout_seconds,
        )
        data = payload.get("data", {})
        rows = data.get("roll_data", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise ValueError("CLS results must be a list")
        collected_at = datetime.now(UTC)
        candidates = []
        for row in rows[: channel.max_results]:
            if not isinstance(row, dict):
                continue
            external_id = plain_text(row.get("id"))
            candidates.append(
                _candidate(
                    channel,
                    request,
                    title=_first(row.get("title"), row.get("brief")),
                    url=f"https://www.cls.cn/detail/{external_id}" if external_id else "",
                    content=_first(row.get("content"), row.get("brief"), row.get("title")),
                    source_name="财联社",
                    source_external_id=external_id or None,
                    published_at=parse_provider_datetime(row.get("ctime")),
                    collected_at=collected_at,
                )
            )
        return [item for item in candidates if item is not None]


class EastmoneyFastAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        payload = await get_json(
            str(channel.endpoint),
            {
                "client": "web",
                "biz": "web_724",
                "fastColumn": "102",
                "sortEnd": "",
                "pageSize": str(channel.max_results),
                "req_trace": str(uuid4()),
            },
            {"Referer": "https://kuaixun.eastmoney.com/"},
            timeout_seconds=channel.timeout_seconds,
        )
        data = payload.get("data", {})
        rows = data.get("fastNewsList", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Eastmoney fast-news results must be a list")
        collected_at = datetime.now(UTC)
        candidates = []
        for row in rows[: channel.max_results]:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("code", "")).strip()
            title = plain_text(row.get("title"))
            candidates.append(
                _candidate(
                    channel,
                    request,
                    title=title,
                    url=f"https://finance.eastmoney.com/a/{external_id}.html" if external_id else "",
                    content=_first(row.get("summary"), title),
                    source_name="东方财富",
                    source_external_id=external_id or None,
                    published_at=parse_china_datetime(row.get("showTime")),
                    collected_at=collected_at,
                )
            )
        return [item for item in candidates if item is not None]


class EastmoneyStockAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        parameter = {
            "uid": "",
            "keyword": request.query,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": channel.max_results,
                }
            },
        }
        payload = await get_text(
            str(channel.endpoint),
            {"cb": "callback", "param": json.dumps(parameter, ensure_ascii=False, separators=(",", ":")), "_": "0"},
            {"Referer": "https://so.eastmoney.com/"},
            timeout_seconds=channel.timeout_seconds,
            max_bytes=2_000_000,
        )
        value = payload.strip()
        if not value.startswith("callback(") or not value.endswith(")"):
            raise ValueError("Eastmoney returned an invalid JSONP envelope")
        decoded = json.loads(value[len("callback(") : -1])
        if not isinstance(decoded, dict) or decoded.get("code") != 0:
            raise ValueError("Eastmoney returned an unsuccessful response")
        result = decoded.get("result", {})
        rows = result.get("cmsArticleWebOld", []) if isinstance(result, dict) else []
        if not isinstance(rows, list):
            raise ValueError("Eastmoney stock-news results must be a list")
        collected_at = datetime.now(UTC)
        candidates = []
        for row in rows[: channel.max_results]:
            if not isinstance(row, dict):
                continue
            candidates.append(
                _candidate(
                    channel,
                    request,
                    title=plain_text(row.get("title")),
                    url=str(row.get("url", "")).strip(),
                    content=_first(row.get("content"), row.get("title")),
                    source_name=plain_text(row.get("mediaName")) or "东方财富",
                    source_external_id=str(row.get("code", "")).strip() or None,
                    published_at=parse_china_datetime(row.get("date")),
                    collected_at=collected_at,
                )
            )
        return [item for item in candidates if item is not None]


class StcnAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        payload = await get_json(
            str(channel.endpoint),
            {"type": "kx"},
            {"Referer": "https://www.stcn.com/article/list/kx.html", "X-Requested-With": "XMLHttpRequest"},
            timeout_seconds=channel.timeout_seconds,
        )
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise ValueError("STCN quick-news results must be a list")
        collected_at = datetime.now(UTC)
        candidates = []
        for row in rows[: channel.max_results]:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("id", "")).strip()
            candidates.append(
                _candidate(
                    channel,
                    request,
                    title=plain_text(row.get("title")),
                    url=urljoin("https://www.stcn.com/", str(row.get("url", "")).strip()),
                    content=_first(row.get("content"), row.get("title")),
                    source_name=plain_text(row.get("source")) or "证券时报",
                    source_external_id=external_id or None,
                    published_at=parse_provider_datetime(row.get("time")),
                    collected_at=collected_at,
                )
            )
        return [item for item in candidates if item is not None]
