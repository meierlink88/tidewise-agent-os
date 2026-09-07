"""Parse model batch rows independently; no semantic corrections or external I/O."""

import json
from typing import Any, get_args

from pydantic import BaseModel, ValidationError


def parse_batch_response(schema: type[BaseModel], content: Any) -> tuple[BaseModel, list[dict]]:
    fields = ("candidates", "no_event") if "candidates" in schema.model_fields else ("events",)
    rejected: list[dict] = []
    if isinstance(content, BaseModel):
        content = content.model_dump(mode="json")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = None
    if not isinstance(content, dict):
        rejected.append({"field": "envelope", "reason": "INVALID_JSON_OBJECT"})
        content = {}
    accepted: dict[str, Any] = {}
    for field in fields:
        accepted[field] = []
        rows = content.get(field)
        if not isinstance(rows, list):
            rejected.append({"field": field, "reason": "MISSING_OR_INVALID_LIST"})
            continue
        item_type = get_args(schema.model_fields[field].annotation)[0]
        limit = next((m.max_length for m in schema.model_fields[field].metadata if hasattr(m, "max_length")), None)
        for index, row in enumerate(rows):
            if limit is not None and len(accepted[field]) >= limit:
                rejected.append({"field": field, "index": index, "reason": "ITEM_EXCEEDS_BATCH_CAPACITY"})
                continue
            try:
                item = item_type.model_validate(row, extra="ignore")
            except ValidationError as error:
                record: dict[str, Any] = {
                    "field": field,
                    "index": index,
                    "reason": "NONCOMPLIANT_MODEL_ITEM",
                    "errors": [
                        {"path": list(e["loc"]), "type": e["type"]}
                        for e in error.errors(include_input=False, include_url=False)
                    ],
                }
                if isinstance(row, dict):
                    if isinstance(row.get("candidate_key"), str):
                        record["candidate_key"] = row["candidate_key"]
                    if isinstance(row.get("evidence_ids"), list):
                        record["evidence_ids"] = [v for v in row["evidence_ids"] if isinstance(v, str)]
                rejected.append(record)
                continue
            accepted[field].append(item)
    # Keep accepted siblings instead of validating the response list atomically.
    return schema.model_construct(**accepted), rejected
