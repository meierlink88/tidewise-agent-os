"""Raw information collection domain modules."""

from capabilities.collection.internal.artifacts import build_artifact_set, publish_artifact_set
from capabilities.collection.internal.buffer import artifact_root
from capabilities.collection.internal.channels.repository import ensure_channel_catalog
from capabilities.collection.internal.models import (
    CollectionQueryPlan,
    CollectionRequest,
    CollectionResult,
    PreparedArtifactSet,
    TitleCurationDraft,
    TitleCurationRequest,
)

__all__ = [
    "CollectionQueryPlan",
    "CollectionRequest",
    "CollectionResult",
    "PreparedArtifactSet",
    "TitleCurationDraft",
    "TitleCurationRequest",
    "artifact_root",
    "build_artifact_set",
    "ensure_channel_catalog",
    "publish_artifact_set",
]
