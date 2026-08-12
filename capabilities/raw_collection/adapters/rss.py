"""Generic RSS 2.0 and Atom Adapter for dynamic feed channels."""

from datetime import UTC, datetime
from uuid import uuid4
from xml.etree import ElementTree

from capabilities.raw_collection.adapters.base import FetchRequest
from capabilities.raw_collection.adapters.common import get_text, parse_provider_datetime, plain_text
from capabilities.raw_collection.channels.models import CollectionChannel
from capabilities.raw_collection.models import Candidate


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
        entries = [item for item in root.iter() if _local_name(item.tag) in {"item", "entry"}]
        collected_at = datetime.now(UTC)
        candidates: list[Candidate] = []
        for entry in entries[: channel.max_results]:
            title = _child_text(entry, "title")
            content = _child_text(entry, "content", "encoded", "summary", "description") or title
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
