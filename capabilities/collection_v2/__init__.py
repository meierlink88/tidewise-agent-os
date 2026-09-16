"""Pure raw collection V2 public output contract."""

from capabilities.collection_v2.internal.models import RawCollectionResultV2

__all__ = ["RawCollectionResultV2"]

from capabilities.collection_v2.internal.models import RawDocumentV2
from capabilities.collection_v2.internal.reader import archived_article_keys, read_archived_article

__all__ += ["RawDocumentV2", "archived_article_keys", "read_archived_article"]
