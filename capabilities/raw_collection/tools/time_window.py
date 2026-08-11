"""Deterministic time-window resolution for collection tool calls."""

import json
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from agno.run import RunContext

SHANGHAI = ZoneInfo("Asia/Shanghai")
_RELATIVE = re.compile(r"(?:最近|近|过去)\s*(\d{1,4})\s*(分钟|分|个?小时|天)")
_DATE_TOKEN = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日?)"
    r"(?:[ T]?\d{1,2}:\d{2}(?::\d{2})?)?"
)
_DATE_RANGE = re.compile(rf"({_DATE_TOKEN})\s*(?:至|到|~|—|－)\s*({_DATE_TOKEN})")


def _parse_local_datetime(value: str, *, end_of_date: bool) -> datetime:
    normalized = re.sub(r"\s+", " ", value.strip())
    match = re.fullmatch(
        r"(\d{4})(?:-|年)(\d{1,2})(?:-|月)(\d{1,2})日?(?:[ T]?(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        normalized,
    )
    if match is None:
        raise ValueError("date expression is invalid")
    year, month, day, hour, minute, second = match.groups()
    parsed = datetime(
        int(year),
        int(month),
        int(day),
        int(hour or 0),
        int(minute or 0),
        int(second or 0),
        tzinfo=SHANGHAI,
    )
    if end_of_date and hour is None:
        parsed += timedelta(days=1)
    return parsed


def resolve_time_window(objective: str, *, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Resolve supported Chinese time expressions against Asia/Shanghai."""
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).replace(microsecond=0)
    explicit = _DATE_RANGE.search(objective)
    if explicit:
        after = _parse_local_datetime(explicit.group(1), end_of_date=False)
        before = _parse_local_datetime(explicit.group(2), end_of_date=True)
        if after >= before:
            raise ValueError("explicit date range is invalid")
        return after, before, "explicit_range"

    relative = _RELATIVE.search(objective)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if amount < 1:
            raise ValueError("relative duration must be positive")
        if unit in {"分钟", "分"}:
            duration = timedelta(minutes=amount)
        elif "小时" in unit:
            duration = timedelta(hours=amount)
        else:
            duration = timedelta(days=amount)
        return current - duration, current, "relative"

    if "今天" in objective:
        return datetime.combine(current.date(), time.min, tzinfo=SHANGHAI), current, "today"
    if "本周" in objective:
        monday = current.date() - timedelta(days=current.weekday())
        return datetime.combine(monday, time.min, tzinfo=SHANGHAI), current, "this_week"
    return current - timedelta(hours=48), current, "default_last_48_hours"


def resolve_collection_time_window(run_context: RunContext) -> str:
    """Return the canonical time window for the current collection objective.

    This must be called once before any channel tool. Copy its `published_after`
    and `published_before` values unchanged into every subsequent channel call.
    """
    objective = (run_context.dependencies or {}).get("collector_objective")
    if not isinstance(objective, str) or not objective.strip():
        return json.dumps({"error": "collection objective is missing"}, ensure_ascii=False)
    try:
        after, before, interpretation = resolve_time_window(objective)
    except ValueError:
        return json.dumps({"error": "time constraint is invalid"}, ensure_ascii=False)
    return json.dumps(
        {
            "timezone": "Asia/Shanghai",
            "interpretation": interpretation,
            "published_after": after.isoformat(),
            "published_before": before.isoformat(),
        },
        ensure_ascii=False,
    )
