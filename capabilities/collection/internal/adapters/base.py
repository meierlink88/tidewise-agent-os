"""Small provider Adapter interface used by channel dispatchers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from capabilities.collection.internal.channels.models import CollectionChannel
from capabilities.collection.internal.models import Candidate


@dataclass(frozen=True)
class FetchRequest:
    query: str
    published_after: datetime
    published_before: datetime


class ChannelAdapter(Protocol):
    async def fetch(self, channel: CollectionChannel, request: FetchRequest) -> list[Candidate]: ...
