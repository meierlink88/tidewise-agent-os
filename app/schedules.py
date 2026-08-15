"""Explicit Schedule seeding and read-only runtime inspection."""

from dataclasses import dataclass
from os import getenv
from typing import Any, Literal

from agno.scheduler import ScheduleManager
from agno.utils.log import log_info, log_warning

from db import get_postgres_db

DEPLOYMENT_CHECK_SCHEDULE_NAME = "deployment-check"
DEPLOYMENT_CHECK_SCHEDULE_ENDPOINT = "/workflows/deployment-check/runs"
RAW_COLLECTION_SCHEDULE_NAME = "raw-collection-hourly"
RAW_COLLECTION_SCHEDULE_ENDPOINT = "/workflows/raw-collection/runs"
RAW_COLLECTION_SCHEDULE_PROMPT = (
    "采集最近48小时可能影响中国A股板块和产业链行情的最新资讯，"
    "关注政策、供需、价格、重大订单、产能投放、技术突破和上市公司经营事件。"
)
EVIDENCE_EXTRACTION_SCHEDULE_NAME = "evidence-extraction-every-10-minutes"
EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT = "/workflows/evidence-extraction/runs"
EVIDENCE_EXTRACTION_SCHEDULE_PROMPT = "处理所有尚未提取的 Raw Document"


@dataclass(frozen=True)
class ScheduleDefinition:
    """Defaults used only when an explicit seed creates a missing Schedule."""

    name: str
    cron: str
    endpoint: str
    payload: dict[str, Any]
    description: str
    timezone: str = "UTC"


@dataclass(frozen=True)
class ScheduleState:
    """Read-only state for one required workflow endpoint."""

    endpoint: str
    names: tuple[str, ...]
    enabled_values: tuple[bool, ...]

    @property
    def status(self) -> Literal["missing", "duplicate", "enabled", "disabled"]:
        if not self.names:
            return "missing"
        if len(self.names) > 1:
            return "duplicate"
        return "enabled" if self.enabled_values[0] else "disabled"

    @property
    def detail(self) -> str:
        if self.status == "missing":
            return f"{self.endpoint} is missing"
        if self.status == "duplicate":
            return f"{self.endpoint} has duplicate Schedules: {', '.join(self.names)}"
        return f"{self.names[0]} ({self.endpoint}) is {self.status}"


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var, accepting 1/true/yes (any casing) as true."""
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes")


def schedule_definitions() -> tuple[ScheduleDefinition, ...]:
    """Return defaults for explicit seeding; they are not runtime authority."""
    definitions: list[ScheduleDefinition] = []
    if env_flag("ENABLE_DEPLOY_CHECK", default=True):
        definitions.append(
            ScheduleDefinition(
                name=DEPLOYMENT_CHECK_SCHEDULE_NAME,
                cron="0 13 * * *",
                endpoint=DEPLOYMENT_CHECK_SCHEDULE_ENDPOINT,
                payload={"message": "Scheduled deployment check."},
                description="Daily: verify platform wiring and readiness.",
            )
        )
    definitions.extend(
        [
            ScheduleDefinition(
                name=RAW_COLLECTION_SCHEDULE_NAME,
                cron="0 * * * *",
                endpoint=RAW_COLLECTION_SCHEDULE_ENDPOINT,
                payload={"message": RAW_COLLECTION_SCHEDULE_PROMPT},
                description="Hourly: collect and publish raw market-moving information from the last 48 hours.",
                timezone="Asia/Shanghai",
            ),
            ScheduleDefinition(
                name=EVIDENCE_EXTRACTION_SCHEDULE_NAME,
                cron="*/10 * * * *",
                endpoint=EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT,
                payload={"message": EVIDENCE_EXTRACTION_SCHEDULE_PROMPT},
                description="Every 10 minutes: extract and publish all indexed, unprocessed Evidence.",
                timezone="Asia/Shanghai",
            ),
        ]
    )
    return tuple(definitions)


def _schedule_value(schedule: object, field: str) -> object:
    if isinstance(schedule, dict):
        return schedule[field]
    return getattr(schedule, field)


def inspect_schedules(*, manager: ScheduleManager | None = None) -> tuple[ScheduleState, ...]:
    """Inspect required Schedules by endpoint without mutating runtime configuration."""
    scheduler = manager or ScheduleManager(get_postgres_db())
    existing = scheduler.list(limit=1_000, page=1)
    states: list[ScheduleState] = []
    for definition in schedule_definitions():
        matches = [schedule for schedule in existing if _schedule_value(schedule, "endpoint") == definition.endpoint]
        states.append(
            ScheduleState(
                endpoint=definition.endpoint,
                names=tuple(str(_schedule_value(schedule, "name")) for schedule in matches),
                enabled_values=tuple(bool(_schedule_value(schedule, "enabled")) for schedule in matches),
            )
        )
    return tuple(states)


def validate_schedules() -> tuple[ScheduleState, ...]:
    """Log read-only startup diagnostics; never create or update a Schedule."""
    try:
        states = inspect_schedules()
    except Exception as exc:
        log_warning(f"schedules: could not inspect runtime configuration: {exc}")
        return ()

    for state in states:
        if state.status == "enabled":
            log_info(f"schedules: {state.detail}")
        else:
            log_warning(
                f"schedules: {state.detail}; manage runtime configuration in Control Panel "
                "or run the explicit seed command for a new environment"
            )
    return states


def seed_schedules(*, manager: ScheduleManager | None = None) -> bool:
    """Create missing Schedule defaults once without changing existing runtime configuration."""
    scheduler = manager or ScheduleManager(get_postgres_db())
    try:
        states = inspect_schedules(manager=scheduler)
    except Exception as exc:
        log_warning(f"schedules: could not inspect runtime configuration before seeding: {exc}")
        return False

    definitions = {definition.endpoint: definition for definition in schedule_definitions()}
    success = True
    for state in states:
        if state.status == "duplicate":
            log_warning(f"schedules: refusing to seed because {state.detail}")
            success = False
            continue
        if state.status != "missing":
            log_info(f"schedules: preserving existing runtime configuration for {state.detail}")
            continue

        definition = definitions[state.endpoint]
        try:
            scheduler.create(
                name=definition.name,
                cron=definition.cron,
                endpoint=definition.endpoint,
                payload=definition.payload,
                description=definition.description,
                timezone=definition.timezone,
                if_exists="raise",
            )
        except Exception as exc:
            log_warning(f"schedules: could not seed '{definition.name}': {exc}")
            success = False
        else:
            log_info(f"schedules: seeded '{definition.name}'")
    return success
