"""Stable Function registry surface."""

from collections.abc import Callable
from typing import Any

from capabilities.event_v2.functions.extraction import (
    document_events_complete,
    extract_next_document_event,
    prepare_document_events,
    summarize_document_events,
)

DOCUMENT_EVENT_FUNCTIONS: list[Callable[..., Any]] = [
    prepare_document_events,
    extract_next_document_event,
    document_events_complete,
    summarize_document_events,
]
__all__ = [
    "DOCUMENT_EVENT_FUNCTIONS",
    "prepare_document_events",
    "extract_next_document_event",
    "document_events_complete",
    "summarize_document_events",
]
