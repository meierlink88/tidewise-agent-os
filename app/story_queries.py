"""REST adapter sharing the public Event capability and AgentOS authentication."""

from fastapi import APIRouter, HTTPException, Request

from capabilities.event import get_story_evidence, query_story_events

router = APIRouter(prefix="/research/story-events", tags=["storyline research"])


def _invoke(fn, request: Request, **kwargs):
    try:
        return fn(**kwargs, user_id=getattr(request.state, "user_id", None))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
def events(request: Request, story_id: str, research_date: str, after_event_id: str = "", limit: int = 100):
    return _invoke(
        query_story_events,
        request,
        story_id=story_id,
        research_date=research_date,
        after_event_id=after_event_id,
        limit=limit,
    )


@router.get("/evidence")
def evidence(request: Request, story_id: str, research_date: str, event_id: str, evidence_id: str):
    return _invoke(
        get_story_evidence,
        request,
        story_id=story_id,
        research_date=research_date,
        event_id=event_id,
        evidence_id=evidence_id,
    )
