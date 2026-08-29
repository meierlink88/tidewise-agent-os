"""Authenticated HTTP boundary for Event Candidate submissions."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sematica.ingestion.episcode.event.contracts import (
    EventCandidateAcceptance,
    EventCandidateRequest,
    EventCandidateStatus,
)
from sematica.ingestion.episcode.event.pipeline import EventCandidatePipeline


def create_router(pipeline: EventCandidatePipeline, *, service_token: str) -> APIRouter:
    router = APIRouter(prefix="/api/reason/v1/event-candidates", tags=["Event candidates"])
    bearer = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None) -> None:
        if (
            credentials is None
            or credentials.scheme.casefold() != "bearer"
            or not hmac.compare_digest(credentials.credentials, service_token)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @router.post(
        "",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=EventCandidateAcceptance,
        operation_id="acceptEventCandidate",
        responses={
            401: {"description": "Missing or invalid service credential"},
            413: {"description": "Request body exceeds the configured limit"},
            422: {"description": "Candidate contract validation failed"},
            500: {"description": "Candidate could not be durably accepted"},
        },
    )
    def accept(
        request: EventCandidateRequest,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> EventCandidateAcceptance:
        authorize(credentials)
        return pipeline.submit(request)

    @router.get(
        "/{submission_id}",
        response_model=EventCandidateStatus,
        operation_id="getEventCandidateStatus",
        responses={
            401: {"description": "Missing or invalid service credential"},
            404: {"description": "Submission was not found"},
            422: {"description": "Submission identity is invalid"},
            500: {"description": "Submission state could not be read"},
        },
    )
    def get_status(
        submission_id: str,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> EventCandidateStatus:
        authorize(credentials)
        result = pipeline.get_status(submission_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return result

    return router
