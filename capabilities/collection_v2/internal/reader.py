"""Public archive-reader implementation for downstream document workflows."""

import re
from pathlib import Path

from capabilities.collection_v2.internal.models import RawArchiveItemV2, RawDocumentV2
from capabilities.collection_v2.internal.storage import artifact_root, sha256


def _directory(article_key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", article_key):
        raise ValueError("Invalid article key")
    return artifact_root() / "articles" / article_key


def archived_article_keys() -> list[str]:
    """Enumerate successful archive identities; bodies are read only when selected."""
    return sorted(p.parent.name for p in (artifact_root() / "articles").glob("*/archived.json"))


def read_archived_article(article_key: str) -> RawDocumentV2:
    directory = _directory(article_key)
    document = RawDocumentV2.model_validate_json((directory / "document.json").read_text())
    receipt = RawArchiveItemV2.model_validate_json((directory / "archived.json").read_text())
    if receipt.status != "archived" or receipt.article_key != article_key or document.article_key != article_key:
        raise ValueError("Archive identity mismatch")
    if receipt.url_path != document.url_path or sha256(document.candidate.content.strip()) != document.content_sha256:
        raise ValueError("Archive source checksum mismatch")
    if sha256((directory / "original.md").read_text()) != document.document_sha256:
        raise ValueError("Archive document checksum mismatch")
    return document
