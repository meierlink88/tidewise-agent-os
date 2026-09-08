"""Raw information collection domain modules."""

from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import artifact_root
from capabilities.collection.internal.models import (
    CollectionRequest,
    CollectionResult,
    PreparedArtifactSet,
    RawEvidenceFilterProgress,
    TitleCurationDraft,
    TitleCurationRequest,
)
from capabilities.collection.internal.source_snapshot import load_active_source_snapshot

__all__ = [
    "CollectionRequest",
    "CollectionResult",
    "PreparedArtifactSet",
    "RawEvidenceFilterProgress",
    "TitleCurationDraft",
    "TitleCurationRequest",
    "artifact_root",
    "build_artifact_set",
    "load_active_source_snapshot",
    "publish_artifact_set",
]

# Stateless acquisition/document interfaces shared with the independent V2 pipeline.
# Existing acquisition, article queues and Workflow execution remain unchanged.
from capabilities.collection.internal.adapters import ChannelAdapter, FetchRequest
from capabilities.collection.internal.adapters.registry import ADAPTERS as SOURCE_ADAPTERS
from capabilities.collection.internal.artifacts import _canonical_url as canonical_source_url
from capabilities.collection.internal.artifacts import _document_markdown as render_raw_markdown
from capabilities.collection.internal.channels import AdapterKey, ChannelType, CollectionChannel
from capabilities.collection.internal.dispatchers.fetch import dispatch_channels
from capabilities.collection.internal.models import Candidate, ChannelFetchReceipt, FetchReceipt, ToolBatch
from capabilities.collection.internal.object_storage import (
    RawDocumentStore,
    configured_raw_document_store,
    raw_evidence_bucket,
)

__all__ += [
    "SOURCE_ADAPTERS",
    "AdapterKey",
    "Candidate",
    "ChannelAdapter",
    "ChannelFetchReceipt",
    "ChannelType",
    "CollectionChannel",
    "FetchReceipt",
    "FetchRequest",
    "RawDocumentStore",
    "ToolBatch",
    "canonical_source_url",
    "configured_raw_document_store",
    "dispatch_channels",
    "raw_evidence_bucket",
    "render_raw_markdown",
]
