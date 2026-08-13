"""Database-managed collection channel catalog."""

from capabilities.collection.internal.channels.models import (
    AdapterKey,
    ChannelCatalog,
    ChannelType,
    CollectionChannel,
    OwnershipType,
)

__all__ = ["AdapterKey", "ChannelCatalog", "ChannelType", "CollectionChannel", "OwnershipType"]
