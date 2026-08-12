"""Small provider Adapter interface used by channel dispatchers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from capabilities.raw_collection.channels.models import CollectionChannel
from capabilities.raw_collection.models import Candidate


@dataclass(frozen=True)
class FetchRequest:
    query: str
    published_after: datetime
    published_before: datetime


class ChannelAdapter(Protocol):
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]: ...
