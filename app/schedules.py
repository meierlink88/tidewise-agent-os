"""
AgentOS Schedules
==================
"""

from os import getenv
from typing import Any

from agno.scheduler import ScheduleManager
from agno.utils.log import log_info, log_warning

from db import get_postgres_db

RAW_COLLECTION_SCHEDULE_NAME = "raw-collection-hourly"
RAW_COLLECTION_SCHEDULE_PROMPT = (
    "采集最近48小时可能影响中国A股板块和产业链行情的最新资讯，"
    "关注政策、供需、价格、重大订单、产能投放、技术突破和上市公司经营事件。"
)


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var, accepting 1/true/yes (any casing) as true."""
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes")


def _register(
    manager: ScheduleManager,
    *,
    name: str,
    cron: str,
    endpoint: str,
    payload: dict[str, Any],
    description: str,
    timezone: str = "UTC",
    enabled: bool = True,
) -> None:
    """Create or update a schedule; failures log a warning instead of crashing the app."""
    try:
        schedule = manager.create(
            name=name,
            cron=cron,
            endpoint=endpoint,
            payload=payload,
            description=description,
            timezone=timezone,
            if_exists="update",
        )
        # A new schedule lands in the DB with updated_at unset; follow-on writes
        # (updates, enable/disable) set it. Unset means this boot created the row.
        created = schedule.updated_at is None
        if created and not enabled:
            disabled = manager.disable(schedule.id)
            if disabled is None or disabled.enabled:
                raise RuntimeError("created enabled but could not be disabled — will retry next boot")
    except Exception as exc:
        log_warning(f"schedules: could not register '{name}': {exc}")
    else:
        if created and not enabled:
            log_info(f"schedules: registered '{name}' (disabled — enable it from the AgentOS UI)")
        else:
            log_info(f"schedules: registered '{name}'")


def register_schedules() -> None:
    """Register schedules (idempotent and fail-soft).

    Raw Collection runs hourly; the deterministic deployment check runs daily by default.
    """
    try:
        manager = ScheduleManager(get_postgres_db())
    except Exception as exc:
        log_warning(f"schedules: could not initialize ScheduleManager: {exc}")
        return

    if env_flag("ENABLE_DEPLOY_CHECK", default=True):
        _register(
            manager,
            name="deployment-check",
            cron="0 13 * * *",  # 13:00 UTC daily
            endpoint="/workflows/deployment-check/runs",
            payload={"message": "Scheduled deployment check."},
            description="Daily: verify platform wiring and readiness.",
        )
    else:
        log_info("schedules: deployment-check disabled (ENABLE_DEPLOY_CHECK=False)")

    _register(
        manager,
        name=RAW_COLLECTION_SCHEDULE_NAME,
        cron="0 * * * *",
        endpoint="/workflows/raw-collection/runs",
        payload={"message": RAW_COLLECTION_SCHEDULE_PROMPT},
        description="Hourly: collect and publish raw market-moving information from the last 48 hours.",
        timezone="Asia/Shanghai",
    )
