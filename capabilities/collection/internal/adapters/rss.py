"""Generic RSS/Atom Adapter with bounded article-body enrichment."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4
from xml.etree import ElementTree

from capabilities.collection.internal.adapters.article import extract_readable_article_text
from capabilities.collection.internal.adapters.base import FetchRequest
from capabilities.collection.internal.adapters.common import get_text, parse_provider_datetime, plain_text
from capabilities.collection.internal.channels.models import CollectionChannel
from capabilities.collection.internal.models import Candidate


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ElementTree.Element, *names: str) -> str:
    expected = set(names)
    for child in element:
        if _local_name(child.tag) in expected:
            return plain_text("".join(child.itertext()))
    return ""


def _entry_link(element: ElementTree.Element) -> str:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href", "")).strip()
        if href:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


class GenericRssAdapter:
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]:
        payload = await get_text(
            str(channel.endpoint),
            {},
            timeout_seconds=channel.timeout_seconds,
            max_bytes=int(channel.config.get("max_bytes", 5_000_000)),
        )
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise ValueError("feed response is not valid XML") from exc
        entries = [item for item in root.iter() if _local_name(item.tag) in {"item", "entry"}][: channel.max_results]
        article_contents = await asyncio.gather(
            *(self._article_content(channel, _entry_link(entry)) for entry in entries),
            return_exceptions=True,
        )
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for entry, article_content in zip(entries, article_contents, strict=True):
            title = _child_text(entry, "title")
            feed_content = _child_text(entry, "content", "encoded", "summary", "description") or title
            content = article_content if isinstance(article_content, str) and article_content else feed_content
            url = _entry_link(entry)
            external_id = _child_text(entry, "guid", "id") or None
            published_at = parse_provider_datetime(_child_text(entry, "published", "updated", "pubdate", "date"))
            try:
                candidates.append(
                    Candidate(
                        candidate_id=str(uuid4()),
                        connector=channel.code,
                        query=request.query,
                        title=title or content[:200],
                        url=url,
                        content=content,
                        source_name=channel.name,
                        source_level=channel.default_source_level,
                        source_external_id=external_id,
                        published_at=published_at,
                        collected_at=collected_at,
                    )
                )
            except ValueError:
                continue
        return candidates

    @staticmethod
    async def _article_content(channel: CollectionChannel, url: str) -> str:
        if not url or channel.config.get("fetch_article_body", True) is False:
            return ""
        maximum_bytes = int(channel.config.get("article_max_bytes", channel.config.get("max_bytes", 5_000_000)))
        timeout_seconds = min(channel.timeout_seconds, int(channel.config.get("article_timeout_seconds", 10)))
        payload = await get_text(
            url,
            {},
            {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
            timeout_seconds=timeout_seconds,
            max_bytes=maximum_bytes,
        )
        extracted = extract_readable_article_text(payload)
        return extracted if len(extracted) >= 40 else ""
