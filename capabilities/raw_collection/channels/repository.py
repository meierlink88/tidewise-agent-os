"""PostgreSQL-owned channel catalog and idempotent fixed-channel seed."""

import os
from datetime import UTC, datetime
from functools import cache
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from capabilities.raw_collection.channels.models import ChannelType, CollectionChannel, OwnershipType
from capabilities.raw_collection.models import SourceLevel
from db.url import db_url

_METADATA = MetaData()
_CHANNELS = Table(
    "collection_channels",
    _METADATA,
    Column("code", String(64), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("ownership_type", String(16), nullable=False),
    Column("channel_type", String(16), nullable=False),
    Column("adapter_key", String(64), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default="false"),
    Column("endpoint", Text, nullable=False),
    Column("app_key", Text),
    Column("config", JSON, nullable=False, server_default="{}"),
    Column("priority", Integer, nullable=False, server_default="1"),
    Column("timeout_seconds", Integer, nullable=False, server_default="30"),
    Column("max_results", Integer, nullable=False, server_default="10"),
    Column("default_source_level", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    ),
    CheckConstraint("ownership_type IN ('fixed', 'dynamic')", name="ck_collection_channels_ownership"),
    CheckConstraint("channel_type IN ('web_search', 'api', 'rss')", name="ck_collection_channels_type"),
    CheckConstraint("priority >= 1", name="ck_collection_channels_priority"),
    CheckConstraint("timeout_seconds BETWEEN 1 AND 300", name="ck_collection_channels_timeout"),
    CheckConstraint("max_results BETWEEN 1 AND 100", name="ck_collection_channels_max_results"),
    CheckConstraint(
        "default_source_level IN ('L1_OFFICIAL', 'L2_WIRE', 'L3_MEDIA', 'L4_SOCIAL')",
        name="ck_collection_channels_source_level",
    ),
)
Index(
    "uq_collection_channels_one_enabled_web_search",
    _CHANNELS.c.channel_type,
    unique=True,
    postgresql_where=_CHANNELS.c.enabled.is_(True) & (_CHANNELS.c.channel_type == ChannelType.WEB_SEARCH.value),
    sqlite_where=_CHANNELS.c.enabled.is_(True) & (_CHANNELS.c.channel_type == ChannelType.WEB_SEARCH.value),
)


def _configured_endpoint(environment_name: str, default: str) -> str:
    return os.getenv(environment_name, default).strip() or default


def _configured_key(environment_name: str) -> str | None:
    return os.getenv(environment_name, "").strip() or None


def _fixed_channel_values() -> list[dict[str, Any]]:
    """Return deployment-aware seed values used only for missing fixed rows."""
    common: dict[str, Any] = {
        "ownership_type": OwnershipType.FIXED.value,
        "priority": 1,
        "timeout_seconds": 30,
        "max_results": 10,
        "config": {},
    }
    return [
        {
            **common,
            "code": "bocha",
            "name": "博查",
            "channel_type": ChannelType.WEB_SEARCH.value,
            "adapter_key": "bocha",
            "enabled": True,
            "endpoint": _configured_endpoint("BOCHA_SEARCH_BASE_URL", "https://api.bochaai.com/v1/web-search"),
            "app_key": _configured_key("BOCHA_API_KEY"),
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
        {
            **common,
            "code": "tavily",
            "name": "Tavily",
            "channel_type": ChannelType.WEB_SEARCH.value,
            "adapter_key": "tavily",
            "enabled": False,
            "endpoint": _configured_endpoint("TAVILY_SEARCH_BASE_URL", "https://api.tavily.com/search"),
            "app_key": _configured_key("TAVILY_API_KEY"),
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
        {
            **common,
            "code": "parallel_search",
            "name": "Parallel Search",
            "channel_type": ChannelType.WEB_SEARCH.value,
            "adapter_key": "parallel",
            "enabled": False,
            "endpoint": _configured_endpoint("PARALLEL_SEARCH_BASE_URL", "https://api.parallel.ai/v1/search"),
            "app_key": _configured_key("PARALLEL_API_KEY"),
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
        {
            **common,
            "code": "cls_telegraph",
            "name": "财联社电报",
            "channel_type": ChannelType.API.value,
            "adapter_key": "cls",
            "enabled": True,
            "endpoint": _configured_endpoint("CLS_TELEGRAPH_BASE_URL", "https://www.cls.cn/v1/roll/get_roll_list"),
            "app_key": None,
            "default_source_level": SourceLevel.L2_WIRE.value,
        },
        {
            **common,
            "code": "eastmoney_fastnews",
            "name": "东方财富 7x24",
            "channel_type": ChannelType.API.value,
            "adapter_key": "eastmoney_fast",
            "enabled": True,
            "endpoint": _configured_endpoint(
                "EASTMONEY_FAST_NEWS_BASE_URL",
                "https://np-weblist.eastmoney.com/comm/web/getFastNewsList",
            ),
            "app_key": None,
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
        {
            **common,
            "code": "eastmoney_stock_news",
            "name": "东方财富个股新闻",
            "channel_type": ChannelType.API.value,
            "adapter_key": "eastmoney_stock",
            "enabled": True,
            "endpoint": _configured_endpoint(
                "EASTMONEY_STOCK_NEWS_BASE_URL",
                "https://search-api-web.eastmoney.com/search/jsonp",
            ),
            "app_key": None,
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
        {
            **common,
            "code": "stcn_quicknews",
            "name": "证券时报快讯",
            "channel_type": ChannelType.API.value,
            "adapter_key": "stcn",
            "enabled": True,
            "endpoint": _configured_endpoint("STCN_QUICK_NEWS_BASE_URL", "https://www.stcn.com/article/list.html"),
            "app_key": None,
            "default_source_level": SourceLevel.L3_MEDIA.value,
        },
    ]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None or value.utcoffset() is None else value


def _decode(row: RowMapping) -> CollectionChannel:
    values = dict(row)
    values["created_at"] = _aware(values["created_at"])
    values["updated_at"] = _aware(values["updated_at"])
    return CollectionChannel.model_validate(values)


def _record(channel: CollectionChannel) -> dict[str, Any]:
    values = channel.model_dump()
    values["endpoint"] = str(channel.endpoint)
    values["ownership_type"] = channel.ownership_type.value
    values["channel_type"] = channel.channel_type.value
    values["default_source_level"] = channel.default_source_level.value
    return values


class ChannelRepository:
    """Deep module owning channel persistence, invariants, and fixed seeds."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def ensure_catalog(self) -> None:
        _METADATA.create_all(self.engine, tables=[_CHANNELS])
        self._install_postgres_guards()
        with self.engine.begin() as connection:
            for values in _fixed_channel_values():
                statement: Any
                if self.engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    statement = pg_insert(_CHANNELS).values(**values).on_conflict_do_nothing(index_elements=["code"])
                elif self.engine.dialect.name == "sqlite":
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                    statement = (
                        sqlite_insert(_CHANNELS).values(**values).on_conflict_do_nothing(index_elements=["code"])
                    )
                else:
                    exists = connection.execute(
                        select(_CHANNELS.c.code).where(_CHANNELS.c.code == values["code"])
                    ).first()
                    if exists is not None:
                        continue
                    statement = insert(_CHANNELS).values(**values)
                connection.execute(statement)

    def _install_postgres_guards(self) -> None:
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.begin() as connection:
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('tidewise_collection_channels_schema'))")
            connection.exec_driver_sql(
                """
                CREATE OR REPLACE FUNCTION guard_collection_channel_identity()
                RETURNS trigger AS $$
                BEGIN
                    IF TG_OP = 'DELETE' AND OLD.ownership_type = 'fixed' THEN
                        RAISE EXCEPTION 'fixed collection channel cannot be deleted';
                    END IF;
                    IF TG_OP = 'UPDATE' AND NEW.code <> OLD.code THEN
                        RAISE EXCEPTION 'collection channel code is immutable';
                    END IF;
                    IF TG_OP = 'UPDATE' AND NEW.ownership_type <> OLD.ownership_type THEN
                        RAISE EXCEPTION 'collection channel ownership is immutable';
                    END IF;
                    IF TG_OP = 'UPDATE' AND OLD.ownership_type = 'fixed'
                       AND (NEW.channel_type <> OLD.channel_type OR NEW.adapter_key <> OLD.adapter_key) THEN
                        RAISE EXCEPTION 'fixed collection channel protocol is immutable';
                    END IF;
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
            connection.exec_driver_sql(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger WHERE tgname = 'collection_channel_identity_guard'
                    ) THEN
                        CREATE TRIGGER collection_channel_identity_guard
                        BEFORE UPDATE OR DELETE ON collection_channels
                        FOR EACH ROW EXECUTE FUNCTION guard_collection_channel_identity();
                    END IF;
                END
                $$
                """
            )

    def list_all(self) -> list[CollectionChannel]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(_CHANNELS).order_by(_CHANNELS.c.priority, _CHANNELS.c.code)).mappings()
            return [_decode(row) for row in rows]

    def list_enabled(self, channel_type: ChannelType) -> list[CollectionChannel]:
        statement = (
            select(_CHANNELS)
            .where(_CHANNELS.c.enabled.is_(True), _CHANNELS.c.channel_type == channel_type.value)
            .order_by(_CHANNELS.c.priority, _CHANNELS.c.code)
        )
        with self.engine.connect() as connection:
            return [_decode(row) for row in connection.execute(statement).mappings()]

    def create_dynamic(self, channel: CollectionChannel) -> None:
        if channel.ownership_type != OwnershipType.DYNAMIC:
            raise ValueError("only dynamic channels can be created through this operation")
        try:
            with self.engine.begin() as connection:
                connection.execute(insert(_CHANNELS).values(**_record(channel)))
        except IntegrityError as exc:
            raise ValueError("channel violates catalog constraints") from exc

    def update_channel(self, code: str, **changes: object) -> CollectionChannel:
        allowed = {
            "name",
            "enabled",
            "endpoint",
            "app_key",
            "config",
            "priority",
            "timeout_seconds",
            "max_results",
            "default_source_level",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("channel update contains unsupported fields")
        existing = next((item for item in self.list_all() if item.code == code), None)
        if existing is None:
            raise ValueError("channel does not exist")
        candidate = CollectionChannel.model_validate(
            {**existing.model_dump(), **changes, "updated_at": datetime.now(UTC)}
        )
        values = _record(candidate)
        values.pop("code")
        values.pop("created_at")
        try:
            with self.engine.begin() as connection:
                connection.execute(update(_CHANNELS).where(_CHANNELS.c.code == code).values(**values))
        except IntegrityError as exc:
            if candidate.channel_type == ChannelType.WEB_SEARCH and candidate.enabled:
                raise ValueError("only one web_search channel may be enabled") from exc
            raise ValueError("channel violates catalog constraints") from exc
        return candidate

    def delete_channel(self, code: str) -> None:
        channel = next((item for item in self.list_all() if item.code == code), None)
        if channel is None:
            return
        if channel.ownership_type == OwnershipType.FIXED:
            raise ValueError("fixed channel cannot be deleted")
        with self.engine.begin() as connection:
            connection.execute(delete(_CHANNELS).where(_CHANNELS.c.code == code))


@cache
def get_channel_repository() -> ChannelRepository:
    return ChannelRepository(create_engine(db_url, pool_pre_ping=True))


def ensure_channel_catalog() -> None:
    """Create the catalog and seed missing fixed channels at runtime startup."""
    get_channel_repository().ensure_catalog()
