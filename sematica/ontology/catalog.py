"""Graphiti-compatible aggregation catalog for Tidewise entity schemas."""

from types import ModuleType
from typing import Any

from pydantic import BaseModel

from sematica.ontology.entities import (
    chain_node,
    company,
    concept,
    country,
    geopolitic_rivalry,
    industry,
    industry_chain,
    macro_economic,
    organization,
    region,
    variable,
)

ONTOLOGY_VERSION = "reasoning-ontology/v5"

_ENTITY_SCHEMAS: tuple[ModuleType, ...] = (
    country,
    region,
    organization,
    industry,
    concept,
    industry_chain,
    chain_node,
    company,
    variable,
    geopolitic_rivalry,
    macro_economic,
)


def _collect_unique(attribute: str) -> dict[Any, Any]:
    """Merge one registration mapping and reject ambiguous schema ownership."""

    result: dict[Any, Any] = {}
    for schema in _ENTITY_SCHEMAS:
        registration = getattr(schema, attribute)
        duplicates = result.keys() & registration.keys()
        if duplicates:
            names = ", ".join(sorted(map(str, duplicates)))
            raise RuntimeError(f"duplicate {attribute} registration: {names}")
        result.update(registration)
    return result


ENTITY_TYPES: dict[str, type[BaseModel]] = _collect_unique("ENTITY_TYPES")
EDGE_TYPES: dict[str, type[BaseModel]] = _collect_unique("EDGE_TYPES")
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = _collect_unique("EDGE_TYPE_MAP")


def ontology_catalog() -> dict[str, Any]:
    """Return a serializable contract for review, logging and prompt provenance."""

    return {
        "version": ONTOLOGY_VERSION,
        "entities": {
            name: {
                "description": model.__doc__,
                "json_schema": model.model_json_schema(),
            }
            for name, model in ENTITY_TYPES.items()
        },
        "entity_links": {
            name: {
                "description": model.__doc__,
                "source_targets": [
                    {"source": source, "target": target}
                    for (source, target), names in EDGE_TYPE_MAP.items()
                    if name in names
                ],
                "json_schema": model.model_json_schema(),
            }
            for name, model in EDGE_TYPES.items()
        },
    }
