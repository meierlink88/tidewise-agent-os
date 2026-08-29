"""Durable Reason-owned Pipeline state for Event Candidate resolution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sematica.ingestion.episcode.event.contracts import (
    DecisionSummary,
    EventCandidateDTO,
    EventCandidateRequest,
    EventCandidateStatus,
    EventResolutionOutcome,
    HistoricalEvent,
)


@dataclass(frozen=True)
class AcceptedSubmission:
    submission_id: str
    replayed: bool


@dataclass(frozen=True)
class ClaimedSubmission:
    submission_id: str
    event: EventCandidateDTO
    evidence_ids: list[str]
    attempt_count: int
    published_event: HistoricalEvent | None
    pending_decision: str | None
    publication_started: bool


class EventCandidateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS event_candidate_submissions (
                    submission_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    event_id TEXT,
                    event_created INTEGER NOT NULL DEFAULT 0,
                    evidence_link_result TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
                    graph_projection_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
                    reason_codes_json TEXT,
                    matched_event_ids_json TEXT,
                    accepted_at TEXT NOT NULL,
                    completed_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    lease_until TEXT,
                    last_error TEXT
                    ,published_event_json TEXT,
                    next_attempt_at TEXT,
                    publication_started INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(event_candidate_submissions)")}
            if "published_event_json" not in columns:
                connection.execute("ALTER TABLE event_candidate_submissions ADD COLUMN published_event_json TEXT")
            if "next_attempt_at" not in columns:
                connection.execute("ALTER TABLE event_candidate_submissions ADD COLUMN next_attempt_at TEXT")
            if "publication_started" not in columns:
                connection.execute(
                    "ALTER TABLE event_candidate_submissions ADD COLUMN publication_started INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def accept(self, request: EventCandidateRequest) -> AcceptedSubmission:
        canonical = request.model_dump(mode="json")
        canonical["evidence_ids"] = sorted(canonical["evidence_ids"])
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT submission_id FROM event_candidate_submissions WHERE request_fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                return AcceptedSubmission(existing["submission_id"], True)
            submission_id = f"evt-submission-{uuid4()}"
            connection.execute(
                """INSERT INTO event_candidate_submissions
                (submission_id, request_fingerprint, payload_json, status, accepted_at)
                VALUES (?, ?, ?, 'ACCEPTED', ?)""",
                (submission_id, fingerprint, payload, datetime.now(UTC).isoformat()),
            )
        return AcceptedSubmission(submission_id, False)

    def get(self, submission_id: str) -> EventCandidateStatus | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM event_candidate_submissions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
        if row is None:
            return None
        summary = None
        if row["reason_codes_json"] is not None:
            summary = DecisionSummary(
                reason_codes=json.loads(row["reason_codes_json"]),
                matched_event_ids=json.loads(row["matched_event_ids_json"]),
            )
        return EventCandidateStatus(
            submission_id=row["submission_id"],
            status=row["status"],
            decision=row["decision"],
            event_id=row["event_id"],
            event_created=bool(row["event_created"]),
            evidence_link_result=row["evidence_link_result"],
            graph_projection_status=row["graph_projection_status"],
            decision_summary=summary,
            accepted_at=datetime.fromisoformat(row["accepted_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    def claim(self, lease_seconds: int = 300) -> ClaimedSubmission | None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM event_candidate_submissions
                WHERE status IN ('ACCEPTED', 'FAILED_RETRYING', 'PUBLISHING', 'PROJECTING', 'RESOLVING')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY accepted_at, submission_id LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            next_status = "PROJECTING" if row["published_event_json"] is not None else "RESOLVING"
            connection.execute(
                """UPDATE event_candidate_submissions SET status=?,
                attempt_count=attempt_count+1, lease_until=?, next_attempt_at=NULL,
                last_error=NULL WHERE submission_id=?""",
                (next_status, (now + timedelta(seconds=lease_seconds)).isoformat(), row["submission_id"]),
            )
            attempt = row["attempt_count"] + 1
        request = EventCandidateRequest.model_validate_json(row["payload_json"])
        published = (
            HistoricalEvent.model_validate_json(row["published_event_json"]) if row["published_event_json"] else None
        )
        return ClaimedSubmission(
            row["submission_id"],
            request.event,
            request.evidence_ids,
            attempt,
            published,
            row["decision"],
            bool(row["publication_started"]),
        )

    def publication_started(self, submission_id: str, decision: str) -> None:
        """Checkpoint the irreversible Data publication intent before HTTP I/O."""

        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions
                   SET status='PUBLISHING', decision=?, publication_started=1
                   WHERE submission_id=?""",
                (decision, submission_id),
            )

    def projection_pending(
        self,
        submission_id: str,
        outcome: EventResolutionOutcome,
        event: HistoricalEvent,
        *,
        terminal: bool,
        retry_delay_seconds: int,
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status=?, decision=?, event_id=?,
                event_created=1, evidence_link_result='CREATED', graph_projection_status='NOT_ATTEMPTED',
                reason_codes_json=?, matched_event_ids_json=?, published_event_json=?, last_error=?,
                lease_until=NULL, next_attempt_at=?, completed_at=? WHERE submission_id=?""",
                (
                    "FAILED" if terminal else "PROJECTING",
                    outcome.decision,
                    event.id,
                    json.dumps(outcome.reason_codes),
                    json.dumps(outcome.matched_event_ids),
                    event.model_dump_json(),
                    "GRAPHITI_EVENT_PROJECTION_FAILED",
                    None if terminal else (now + timedelta(seconds=retry_delay_seconds)).isoformat(),
                    now.isoformat() if terminal else None,
                    submission_id,
                ),
            )

    def analysis_scheduling_pending(
        self,
        submission_id: str,
        outcome: EventResolutionOutcome,
        event: HistoricalEvent,
        *,
        terminal: bool,
        retry_delay_seconds: int,
    ) -> None:
        """Retry analysis enqueue without misreporting the completed graph projection."""

        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status=?, decision=?, event_id=?,
                    event_created=1, evidence_link_result='CREATED',
                    graph_projection_status='SUCCEEDED', reason_codes_json=?,
                    matched_event_ids_json=?, published_event_json=?, last_error=?,
                    lease_until=NULL, next_attempt_at=?, completed_at=? WHERE submission_id=?""",
                (
                    "FAILED" if terminal else "PROJECTING",
                    outcome.decision,
                    event.id,
                    json.dumps(outcome.reason_codes),
                    json.dumps(outcome.matched_event_ids),
                    event.model_dump_json(),
                    "EVENT_ANALYSIS_SCHEDULING_FAILED",
                    None if terminal else (now + timedelta(seconds=retry_delay_seconds)).isoformat(),
                    now.isoformat() if terminal else None,
                    submission_id,
                ),
            )

    def published(
        self,
        submission_id: str,
        outcome: EventResolutionOutcome,
        event: HistoricalEvent,
    ) -> None:
        """Durably record the formal Data Event before any Graphiti side effect."""

        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status='PROJECTING',
                    decision=?, event_id=?, event_created=1,
                    evidence_link_result='CREATED', graph_projection_status='NOT_ATTEMPTED',
                    reason_codes_json=?, matched_event_ids_json=?, published_event_json=?
                    WHERE submission_id=?""",
                (
                    outcome.decision,
                    event.id,
                    json.dumps(outcome.reason_codes),
                    json.dumps(outcome.matched_event_ids),
                    event.model_dump_json(),
                    submission_id,
                ),
            )

    def needs_review(self, submission_id: str, error_code: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status='NEEDS_REVIEW',
                    decision='NEEDS_REVIEW', event_created=0,
                    evidence_link_result='NOT_ATTEMPTED',
                    graph_projection_status='NOT_ATTEMPTED',
                    reason_codes_json=?, matched_event_ids_json='[]',
                    last_error=?, completed_at=?, lease_until=NULL,
                    next_attempt_at=NULL WHERE submission_id=?""",
                (json.dumps([error_code]), error_code, now, submission_id),
            )

    def complete(self, submission_id: str, outcome: EventResolutionOutcome) -> None:
        status = "NEEDS_REVIEW" if outcome.decision in {"NEEDS_REVIEW", "SAME_EVENT_REVISION"} else "SUCCEEDED"
        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status=?, decision=?, event_id=?,
                event_created=?, evidence_link_result=?, graph_projection_status=?, reason_codes_json=?,
                matched_event_ids_json=?, completed_at=?, lease_until=NULL WHERE submission_id=?""",
                (
                    status,
                    outcome.decision,
                    outcome.event_id,
                    int(outcome.event_created),
                    outcome.evidence_link_result,
                    outcome.graph_projection_status,
                    json.dumps(outcome.reason_codes),
                    json.dumps(outcome.matched_event_ids),
                    datetime.now(UTC).isoformat(),
                    submission_id,
                ),
            )

    def fail(self, submission_id: str, error_code: str, *, terminal: bool, retry_delay_seconds: int) -> None:
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """UPDATE event_candidate_submissions SET status=?, last_error=?,
                lease_until=NULL, next_attempt_at=?, completed_at=? WHERE submission_id=?""",
                (
                    "FAILED" if terminal else "FAILED_RETRYING",
                    error_code,
                    None if terminal else (now + timedelta(seconds=retry_delay_seconds)).isoformat(),
                    now.isoformat() if terminal else None,
                    submission_id,
                ),
            )
