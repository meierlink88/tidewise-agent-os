"""Durable per-article checkpoints and shared-volume duplicate-decision lock."""

import asyncio
import fcntl
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile


def root() -> Path:
    return Path(os.getenv("EVENT_V2_ARTIFACT_ROOT", "data/event_v2")).resolve()


def path(kind: str, key: str, name: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", key):
        raise ValueError("Unsafe Event artifact identity")
    return root() / kind / key / f"{name}.json"


def read(file: Path) -> dict | None:
    return json.loads(file.read_text()) if file.exists() else None


def write(file: Path, value: dict) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=file.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
            os.replace(temp, file)
        finally:
            temp.unlink(missing_ok=True)
    fd = os.open(file.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@asynccontextmanager
async def decision_lock():
    """Serialize recall/decision/staging across workers sharing this artifact volume."""
    root().mkdir(parents=True, exist_ok=True)
    with (root() / ".decision.lock").open("a") as handle:
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.2)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
