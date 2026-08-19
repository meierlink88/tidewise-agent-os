"""Strict, bounded consumer of the Data Service Source Snapshot contract."""

import json
import os
from datetime import datetime
from typing import Any, Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from capabilities.collection.internal.channels.models import (
    AdapterKey,
    ChannelType,
    CollectionChannel,
    OwnershipType,
)
from capabilities.collection.internal.models import SourceLevel

_SNAPSHOT_PATH = "/api/data/v1/source-snapshot"
_MAX_RESPONSE_BYTES = 500_000
_MAX_SOURCES = 200
_CLIENT_TIMEOUT_SECONDS = 3.5


class SourceSnapshotProvider(Protocol):
    """Read-only boundary consumed once at Raw Collection startup."""

    def load_active_snapshot(self) -> tuple[CollectionChannel, ...]: ...


class _SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr = Field(
        pattern=r"^SRC[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        max_length=39,
    )
    code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$", min_length=1, max_length=64)
    name: StrictStr = Field(min_length=1, max_length=100)
    ownership_type: OwnershipType
    channel_type: ChannelType
    adapter_key: AdapterKey
    enabled: StrictBool
    endpoint: HttpUrl
    app_key: StrictStr | None = Field(max_length=512)
    config: dict[StrictStr, Any]
    priority: StrictInt = Field(ge=1, le=5)
    timeout_seconds: StrictInt = Field(ge=1, le=300)
    max_results: StrictInt = Field(ge=1, le=100)
    default_source_level: SourceLevel
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_source_contract(self) -> "_SourceRecord":
        if self.app_key is not None and not self.app_key.strip():
            raise ValueError("app_key must be nonblank when present")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        self._validate_config()
        return self

    def _validate_config(self) -> None:
        source_levels = self.config.get("source_levels")
        if source_levels is not None:
            if not isinstance(source_levels, dict):
                raise ValueError("config.source_levels must be an object")
            valid_levels = {level.value for level in SourceLevel}
            if any(
                not isinstance(host, str) or not host.strip() or not isinstance(level, str) or level not in valid_levels
                for host, level in source_levels.items()
            ):
                raise ValueError("config.source_levels contains an invalid entry")
        maximum = self.config.get("max_bytes")
        if maximum is not None and (
            not isinstance(maximum, int) or isinstance(maximum, bool) or not 65_536 <= maximum <= 10_485_760
        ):
            raise ValueError("config.max_bytes is outside the allowed range")

    def as_channel(self) -> CollectionChannel:
        values = self.model_dump(exclude={"id"})
        return CollectionChannel.model_validate(values)


class _SnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[_SourceRecord, ...] = Field(max_length=_MAX_SOURCES)


class _SnapshotEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: StrictStr = Field(min_length=1)
    result: _SnapshotResult


class DataServiceSourceSnapshotProvider:
    """Load and validate one complete active Source Snapshot from Data Service."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = _CLIENT_TIMEOUT_SECONDS) -> None:
        normalized_url = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("DATA_SERVICE_BASE_URL is invalid")
        if not token.strip():
            raise ValueError("DATA_SERVICE_TOKEN is not configured")
        self._base_url = normalized_url
        self._token = token.strip()
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> Self:
        return cls(
            base_url=os.getenv("DATA_SERVICE_BASE_URL", "http://data:9011"),
            token=os.getenv("DATA_SERVICE_TOKEN", ""),
        )

    def load_active_snapshot(self) -> tuple[CollectionChannel, ...]:
        request = Request(
            f"{self._base_url}{_SNAPSHOT_PATH}",
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise ValueError(f"Data Service Source Snapshot returned unexpected HTTP {response.status}")
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise ValueError(self._http_error_message(exc)) from None
        except (TimeoutError, URLError):
            raise ValueError("Data Service Source Snapshot is unavailable within the request budget") from None
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise ValueError("Data Service Source Snapshot exceeds the 500000-byte contract limit")
        try:
            envelope = _SnapshotEnvelope.model_validate_json(payload)
            self._validate_complete_snapshot(envelope.result.sources)
            return tuple(source.as_channel().model_copy(deep=True) for source in envelope.result.sources)
        except (ValidationError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Data Service Source Snapshot violates the complete snapshot contract") from None

    @staticmethod
    def _validate_complete_snapshot(sources: tuple[_SourceRecord, ...]) -> None:
        if any(not source.enabled for source in sources):
            raise ValueError("snapshot contains an inactive Source")
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("snapshot contains duplicate Source IDs")
        if len({source.code for source in sources}) != len(sources):
            raise ValueError("snapshot contains duplicate Source codes")
        expected = tuple(
            sorted(sources, key=lambda source: (source.channel_type.value, source.priority, source.code, source.id))
        )
        if sources != expected:
            raise ValueError("snapshot ordering is invalid")
        if sum(source.channel_type == ChannelType.WEB_SEARCH for source in sources) > 1:
            raise ValueError("snapshot contains multiple web-search Sources")
        for source in sources:
            encoded_config = json.dumps(source.config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded_config) > 4096:
                raise ValueError("snapshot Source config is too large")

    @staticmethod
    def _http_error_message(exc: HTTPError) -> str:
        code = "UNKNOWN"
        request_id = "unknown"
        try:
            payload = json.loads(exc.read(65_537))
            if isinstance(payload, dict):
                raw_request_id = payload.get("request_id")
                error = payload.get("error")
                raw_code = error.get("code") if isinstance(error, dict) else None
                if isinstance(raw_request_id, str) and raw_request_id.strip():
                    request_id = raw_request_id.strip()
                if isinstance(raw_code, str) and raw_code.strip():
                    code = raw_code.strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return f"Data Service Source Snapshot failed with HTTP {exc.code}: {code} (request_id={request_id})"


def load_active_source_snapshot(
    provider: SourceSnapshotProvider | None = None,
) -> tuple[CollectionChannel, ...]:
    """Load one complete Snapshot using the process Data Service configuration."""
    resolved = provider or DataServiceSourceSnapshotProvider.from_environment()
    return resolved.load_active_snapshot()
