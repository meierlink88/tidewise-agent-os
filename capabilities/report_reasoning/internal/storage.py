"""Hashing and atomic artifact writes; no execution locks."""

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
