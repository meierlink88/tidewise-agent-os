"""Strict, bounded consumer of the Data Service Source Snapshot contract."""

import json
import os
import re
from copy import deepcopy
from datetime import datetime
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
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
_READ_CHUNK_BYTES = 64 * 1024
_DATA_REQUEST_ID_PATTERN = re.compile(r"^data-[0-9]{8}T[0-9]{6}\.[0-9]{9}$")
_RFC3339_DATETIME_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
_SOURCE_ERROR_CODES = {
    "FORBIDDEN",
    "INTERNAL_ERROR",
    "INVALID_REQUEST",
    "SOURCE_FAILED",
    "SOURCE_SNAPSHOT_FAILED",
    "SOURCE_TIMEOUT",
    "UNAUTHENTICATED",
}


class SourceSnapshotErrorKind(StrEnum):
    CONFIGURATION = "configuration"
    HTTP = "http"
    INVALID = "invalid"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"


class SourceSnapshotError(ValueError):
    """Sanitized failure classification for deterministic Workflow handling."""

    def __init__(self, kind: SourceSnapshotErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def _open_request(request: Request, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


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
    endpoint: AnyUrl = Field(max_length=2048)
    app_key: StrictStr | None = Field(max_length=512)
    config: dict[StrictStr, Any]
    priority: StrictInt = Field(ge=1, le=5)
    timeout_seconds: StrictInt = Field(ge=1, le=300)
    max_results: StrictInt = Field(ge=1, le=100)
    default_source_level: SourceLevel
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_rfc3339_datetime(cls, value: Any) -> Any:
        if not isinstance(value, str) or _RFC3339_DATETIME_PATTERN.fullmatch(value) is None:
            raise ValueError("timestamp must use RFC3339 date-time syntax")
        return value

    @model_validator(mode="after")
    def validate_source_contract(self) -> "_SourceRecord":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        self._validate_config()
        return self

    def _validate_config(self) -> None:
        source_levels = self.config.get("source_levels")
        if source_levels is not None:
            if not isinstance(source_levels, dict):
                raise ValueError("config.source_levels must be an object")
            valid_levels = {level.value for level in SourceLevel}
            if any(not isinstance(level, str) or level not in valid_levels for level in source_levels.values()):
                raise ValueError("config.source_levels contains an invalid entry")
        maximum = self.config.get("max_bytes")
        if (
            self.channel_type == ChannelType.RSS
            and maximum is not None
            and (not isinstance(maximum, int) or isinstance(maximum, bool) or not 65_536 <= maximum <= 10_485_760)
        ):
            raise ValueError("config.max_bytes is outside the allowed range")


class _SnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: tuple[_SourceRecord, ...] = Field(max_length=_MAX_SOURCES)


class _SnapshotEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: StrictStr = Field(min_length=1, max_length=128)
    result: _SnapshotResult


class _ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: StrictStr = Field(min_length=1, max_length=100)
    message: StrictStr = Field(min_length=1, max_length=500)
    details: dict[StrictStr, Any]


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: _ErrorDetail
    request_id: StrictStr = Field(min_length=1, max_length=128)


def _map_source_to_execution_channel(source: _SourceRecord) -> CollectionChannel:
    """Map the wire contract explicitly while leaving Data Source ID out of execution identity."""
    return CollectionChannel(
        code=source.code,
        name=source.name,
        ownership_type=source.ownership_type,
        channel_type=source.channel_type,
        adapter_key=source.adapter_key,
        enabled=source.enabled,
        endpoint=str(source.endpoint),
        app_key=source.app_key,
        config=deepcopy(source.config),
        priority=source.priority,
        timeout_seconds=source.timeout_seconds,
        max_results=source.max_results,
        default_source_level=source.default_source_level,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


class DataServiceSourceSnapshotProvider:
    """Load and validate one complete active Source Snapshot from Data Service."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = _CLIENT_TIMEOUT_SECONDS) -> None:
        normalized_url = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SourceSnapshotError(SourceSnapshotErrorKind.CONFIGURATION, "DATA_SERVICE_BASE_URL is invalid")
        if not token.strip():
            raise SourceSnapshotError(
                SourceSnapshotErrorKind.CONFIGURATION,
                "DATA_SERVICE_TOKEN is not configured",
            )
        if timeout_seconds <= 0:
            raise SourceSnapshotError(
                SourceSnapshotErrorKind.CONFIGURATION,
                "Source Snapshot timeout must be positive",
            )
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
        deadline = monotonic() + self._timeout_seconds
        try:
            with _open_request(request, timeout=self._timeout_seconds) as response:
                if response.status != 200:
                    raise SourceSnapshotError(
                        SourceSnapshotErrorKind.HTTP,
                        f"Data Service Source Snapshot returned unexpected HTTP {response.status}",
                    )
                payload = self._read_response(response, deadline)
        except HTTPError as exc:
            try:
                message = self._http_error_message(exc, deadline)
            except TimeoutError:
                raise SourceSnapshotError(
                    SourceSnapshotErrorKind.UNAVAILABLE,
                    "Data Service Source Snapshot is unavailable within the request budget",
                ) from None
            finally:
                exc.close()
            raise SourceSnapshotError(SourceSnapshotErrorKind.HTTP, message) from None
        except (TimeoutError, URLError):
            raise SourceSnapshotError(
                SourceSnapshotErrorKind.UNAVAILABLE,
                "Data Service Source Snapshot is unavailable within the request budget",
            ) from None
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise SourceSnapshotError(
                SourceSnapshotErrorKind.TOO_LARGE,
                "Data Service Source Snapshot exceeds the 500000-byte contract limit",
            )
        try:
            envelope = _SnapshotEnvelope.model_validate_json(payload)
            self._validate_complete_snapshot(envelope.result.sources)
            return tuple(_map_source_to_execution_channel(source) for source in envelope.result.sources)
        except (ValidationError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise SourceSnapshotError(
                SourceSnapshotErrorKind.INVALID,
                "Data Service Source Snapshot violates the complete snapshot contract",
            ) from None

    @staticmethod
    def _read_response(response: Any, deadline: float, *, maximum_bytes: int = _MAX_RESPONSE_BYTES) -> bytes:
        chunks: list[bytes] = []
        total = 0
        reader = getattr(response, "read1", response.read)
        while True:
            if chunks and DataServiceSourceSnapshotProvider._response_is_closed(response):
                return b"".join(chunks)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            DataServiceSourceSnapshotProvider._set_socket_timeout(response, remaining)
            chunk = reader(min(_READ_CHUNK_BYTES, maximum_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                return b"".join(chunks)

    @staticmethod
    def _response_is_closed(response: Any) -> bool:
        current = response
        for _ in range(4):
            isclosed = getattr(current, "isclosed", None)
            if callable(isclosed) and isclosed():
                return True
            current = getattr(current, "fp", None)
            if current is None:
                return False
        return False

    @staticmethod
    def _set_socket_timeout(response: Any, timeout_seconds: float) -> None:
        current = response
        for _ in range(4):
            raw = getattr(current, "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                sock.settimeout(timeout_seconds)
                return
            current = getattr(current, "fp", None)
            if current is None:
                break
        raise TimeoutError

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
    def _http_error_message(exc: HTTPError, deadline: float) -> str:
        code = "UNKNOWN"
        request_id = "unknown"
        try:
            payload = DataServiceSourceSnapshotProvider._read_response(exc, deadline, maximum_bytes=65_536)
            envelope = _ErrorEnvelope.model_validate_json(payload)
            if envelope.error.code in _SOURCE_ERROR_CODES:
                code = envelope.error.code
            if _DATA_REQUEST_ID_PATTERN.fullmatch(envelope.request_id):
                request_id = envelope.request_id
        except (ValidationError, UnicodeDecodeError):
            pass
        return f"Data Service Source Snapshot failed with HTTP {exc.code}: {code} (request_id={request_id})"


def load_active_source_snapshot(
    provider: SourceSnapshotProvider | None = None,
) -> tuple[CollectionChannel, ...]:
    """Load one complete Snapshot using the process Data Service configuration."""
    resolved = provider or DataServiceSourceSnapshotProvider.from_environment()
    return resolved.load_active_snapshot()
