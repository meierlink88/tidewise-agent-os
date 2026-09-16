"""Stable Function registry surface."""

from collections.abc import Callable
from typing import Any

from capabilities.event_v2.functions.extraction import (
    associate_document_story,
    discover_document_signals,
    document_events_complete,
    extract_next_document_event,
    prepare_document_events,
    publish_document_event,
    summarize_document_events,
)

DOCUMENT_EVENT_FUNCTIONS: list[Callable[..., Any]] = [
    prepare_document_events,
    extract_next_document_event,
    associate_document_story,
    discover_document_signals,
    publish_document_event,
    document_events_complete,
    summarize_document_events,
]
__all__ = [
    "DOCUMENT_EVENT_FUNCTIONS",
    "prepare_document_events",
    "extract_next_document_event",
    "document_events_complete",
    "associate_document_story",
    "discover_document_signals",
    "publish_document_event",
    "summarize_document_events",
]
