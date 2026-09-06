"""Explicit local cutover: stop the old scheduled consumer and transfer its backlog."""

import argparse
import json

from agno.scheduler import ScheduleManager

from app.schedules import EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT, RAW_COLLECTION_SCHEDULE_ENDPOINT
from capabilities.collection.functions import import_legacy_articles
from db import get_postgres_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--consumers-stopped",
        action="store_true",
        required=True,
        help="Confirm no Raw Collection or Evidence Extraction run is active during transfer",
    )
    parser.parse_args()
    manager = ScheduleManager(get_postgres_db())
    schedules = manager.list(limit=1000, page=1)
    if any(s.endpoint == RAW_COLLECTION_SCHEDULE_ENDPOINT and s.enabled for s in schedules):
        raise RuntimeError("Pause the Raw Collection schedule before cutover; its prior state is not changed here")
    disabled = []
    for schedule in schedules:
        if schedule.endpoint == EVIDENCE_EXTRACTION_SCHEDULE_ENDPOINT and schedule.enabled:
            manager.disable(schedule.id)
            disabled.append(schedule.id)
    print(json.dumps({"disabled_legacy_schedules": disabled, **import_legacy_articles()}))


if __name__ == "__main__":
    main()
