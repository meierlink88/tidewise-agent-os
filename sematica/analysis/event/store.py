"""Durable, idempotent workflow state for Event Analysis."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from sematica.analysis.event.contracts import (
    EventAnalysisAcceptance,
    EventAnalysisInput,
    EventAnalysisOutcome,
    EventAnalysisStatus,
    EventClassification,
)

PIPELINE_VERSION = "event-analysis/v1"


@dataclass(frozen=True)
class ClaimedAnalysis:
    analysis_id: str
    input: EventAnalysisInput
    attempt_count: int


class EventAnalysisStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS event_analysis_runs (
                    analysis_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    classification_json TEXT,
                    signal_fact_uuids_json TEXT NOT NULL DEFAULT '[]',
                    reason_codes_json TEXT NOT NULL DEFAULT '[]',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    accepted_at TEXT NOT NULL,
                    completed_at TEXT,
                    lease_until TEXT,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    UNIQUE(event_id, pipeline_version)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def enqueue(self, input_: EventAnalysisInput) -> EventAnalysisAcceptance:
        analysis_id = str(
            uuid5(
                NAMESPACE_URL,
                f"urn:tidewise:event-analysis:{PIPELINE_VERSION}:{input_.event.id}",
            )
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT analysis_id FROM event_analysis_runs WHERE event_id=? AND pipeline_version=?",
                (input_.event.id, PIPELINE_VERSION),
            ).fetchone()
            if existing is not None:
                return EventAnalysisAcceptance(
                    analysis_id=str(existing["analysis_id"]),
                    event_id=input_.event.id,
                    replayed=True,
                )
            connection.execute(
                """
                INSERT INTO event_analysis_runs
                    (analysis_id, event_id, pipeline_version, input_json, status, accepted_at)
                VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    analysis_id,
                    input_.event.id,
                    PIPELINE_VERSION,
                    input_.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return EventAnalysisAcceptance(analysis_id=analysis_id, event_id=input_.event.id, replayed=False)

    def claim(self, lease_seconds: int = 300) -> ClaimedAnalysis | None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM event_analysis_runs
                WHERE status IN (
                    'PENDING', 'FAILED_RETRYING', 'CLASSIFYING', 'GROUNDING',
                    'EXTRACTING', 'VALIDATING', 'PROJECTING'
                )
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY CASE status
                           WHEN 'PENDING' THEN 0
                           WHEN 'FAILED_RETRYING' THEN 1
                           ELSE 2
                         END,
                         accepted_at, analysis_id
                LIMIT 1
                """,
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            attempt_count = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE event_analysis_runs
                SET status='CLASSIFYING', attempt_count=?, lease_until=?,
                    next_attempt_at=NULL, last_error=NULL
                WHERE analysis_id=?
                """,
                (
                    attempt_count,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    row["analysis_id"],
                ),
            )
        return ClaimedAnalysis(
            analysis_id=str(row["analysis_id"]),
            input=EventAnalysisInput.model_validate_json(row["input_json"]),
            attempt_count=attempt_count,
        )

    def set_stage(self, analysis_id: str, stage: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE event_analysis_runs SET status=? WHERE analysis_id=?",
                (stage, analysis_id),
            )

    def complete(self, analysis_id: str, outcome: EventAnalysisOutcome) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE event_analysis_runs
                SET status=?, classification_json=?, signal_fact_uuids_json=?,
                    reason_codes_json=?, completed_at=?, lease_until=NULL,
                    next_attempt_at=NULL, last_error=NULL
                WHERE analysis_id=?
                """,
                (
                    outcome.status,
                    outcome.classification.model_dump_json(),
                    json.dumps(outcome.signal_fact_uuids),
                    json.dumps(outcome.reason_codes),
                    datetime.now(UTC).isoformat(),
                    analysis_id,
                ),
            )

    def fail(
        self,
        analysis_id: str,
        *,
        terminal: bool,
        retry_delay_seconds: int,
        error_code: str = "EVENT_ANALYSIS_FAILED",
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE event_analysis_runs
                SET status=?, last_error=?, lease_until=NULL,
                    next_attempt_at=?, completed_at=?
                WHERE analysis_id=?
                """,
                (
                    "FAILED" if terminal else "FAILED_RETRYING",
                    error_code,
                    None if terminal else (now + timedelta(seconds=retry_delay_seconds)).isoformat(),
                    now.isoformat() if terminal else None,
                    analysis_id,
                ),
            )

    def get(self, analysis_id: str) -> EventAnalysisStatus | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM event_analysis_runs WHERE analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            return None
        classification = (
            EventClassification.model_validate_json(row["classification_json"]) if row["classification_json"] else None
        )
        return EventAnalysisStatus(
            analysis_id=str(row["analysis_id"]),
            event_id=str(row["event_id"]),
            status=cast(Any, str(row["status"])),
            classification=classification,
            signal_fact_uuids=json.loads(row["signal_fact_uuids_json"]),
            reason_codes=json.loads(row["reason_codes_json"]),
            attempt_count=int(row["attempt_count"]),
            accepted_at=datetime.fromisoformat(row["accepted_at"]),
            completed_at=(datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None),
            last_error=row["last_error"],
        )
