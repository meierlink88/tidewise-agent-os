"""Independent acquisition and archive contracts; no Evidence processing state."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from capabilities.collection import Candidate, FetchReceipt, ToolBatch


class RawAcquisitionV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["raw_collection_v2_acquisition.v1"] = "raw_collection_v2_acquisition.v1"
    collection_id: str
    query: str
    receipts: list[FetchReceipt]
    batches: list[ToolBatch]


class RawDocumentV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["raw_collection_v2_document.v1"] = "raw_collection_v2_document.v1"
    article_key: str
    publication_key: str
    canonical_url: str
    content_sha256: str
    document_sha256: str
    bucket: str
    object_key: str
    url_path: str
    candidate: Candidate


class RawArchiveItemV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    status: Literal["archived", "duplicate", "failed", "invalid"]
    article_key: str | None = None
    url_path: str | None = None
    error_code: str | None = None


class RawCollectionResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["raw_collection_v2_result.v1"] = "raw_collection_v2_result.v1"
    collection_id: str
    outcome: Literal["completed", "partial", "failed", "no_change"]
    archived: int
    duplicates: int
    failed: int
    invalid: int
    failed_channels: int
    manifest_path: str
    completed_at: datetime
