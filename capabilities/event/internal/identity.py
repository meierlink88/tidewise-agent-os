"""Deterministic exact-identity guardrails for formal Events."""

from __future__ import annotations

import re
from typing import Any


def _term(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _terms(values: list[str]) -> set[str]:
    return {_term(value) for value in values}


def _time(event: Any) -> Any:
    return event.semantic.time.occurred_at or event.semantic.time.announced_at or event.semantic.time.effective_at


def same_occurrence(candidate: Any, historical: Any) -> bool:
    """Return true only for the same exact formal occurrence dimensions."""

    left, right = candidate.semantic, historical.semantic
    return (
        _terms(left.actors) == _terms(right.actors)
        and _term(left.action) == _term(right.action)
        and _terms(left.objects) == _terms(right.objects)
        and left.stage == right.stage
        and _time(candidate) == _time(historical)
    )
