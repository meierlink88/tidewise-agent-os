"""Process-local immutable channel snapshots for one Workflow run."""

import threading
from datetime import UTC, datetime, timedelta

from capabilities.raw_collection.channels.models import ChannelType, CollectionChannel

_MAX_AGE = timedelta(hours=24)
_LOCK = threading.Lock()
_SNAPSHOTS: dict[str, tuple[datetime, tuple[CollectionChannel, ...]]] = {}


def freeze_channel_snapshot(run_id: str, channels: list[CollectionChannel]) -> None:
    """Freeze enabled channel configuration before semantic planning begins."""
    now = datetime.now(UTC)
    immutable = tuple(sorted((item.model_copy(deep=True) for item in channels if item.enabled), key=lambda x: x.code))
    with _LOCK:
        expired = [key for key, (created_at, _) in _SNAPSHOTS.items() if now - created_at > _MAX_AGE]
        for key in expired:
            _SNAPSHOTS.pop(key, None)
        existing = _SNAPSHOTS.get(run_id)
        if existing is not None and existing[1] != immutable:
            raise ValueError("collection channel snapshot identity conflict")
        _SNAPSHOTS[run_id] = (now, immutable)


def list_snapshot_channels(run_id: str, channel_type: ChannelType) -> list[CollectionChannel]:
    with _LOCK:
        snapshot = _SNAPSHOTS.get(run_id)
    if snapshot is None:
        raise ValueError("collection channel snapshot is missing")
    return [item for item in snapshot[1] if item.channel_type == channel_type]


def release_channel_snapshot(run_id: str) -> None:
    with _LOCK:
        _SNAPSHOTS.pop(run_id, None)
