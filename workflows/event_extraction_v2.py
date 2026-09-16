"""Independent document Event preparation and sequential Function loop."""

from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.event_v2.functions import (
    document_events_complete,
    extract_next_document_event,
    prepare_document_events,
    summarize_document_events,
)
from db import get_postgres_db

DOCUMENT_EVENT_WORKFLOW_ID = "event-extraction-v2"


def build_document_event_workflow() -> Workflow:
    def step(name, function):
        return Step(name=name, executor=function, max_retries=0, human_review=HumanReview(on_error=OnError.fail))

    return Workflow(
        id=DOCUMENT_EVENT_WORKFLOW_ID,
        name="事件提取",
        db=get_postgres_db(),
        description="每批20篇原文，逐篇提取文档Event，向量召回后模型判重；仅保留待发布候选。",
        steps=[
            step("准备数据", prepare_document_events),
            Loop(
                name="逐篇处理原文",
                steps=[step("Event 提取", extract_next_document_event)],
                end_condition=document_events_complete,
                max_iterations=20,
                forward_iteration_output=False,
                human_review=HumanReview(on_error=OnError.fail),
            ),
            step("汇总提取结果", summarize_document_events),
        ],
    )


def ensure_document_event_workflow(registry: Registry) -> int:
    db = get_postgres_db()
    component = db.get_component(DOCUMENT_EVENT_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is None:
        version = build_document_event_workflow().save(
            db=db, stage="published", notes="Initial document Event extraction only; no publication"
        )
    else:
        version = component.get("current_version")
        if Workflow.load(DOCUMENT_EVENT_WORKFLOW_ID, db=db, registry=registry, version=version) is None:
            raise ValueError("Document Event Workflow cannot be loaded")
    if not isinstance(version, int):
        raise ValueError("Document Event Workflow has no published version")
    return version
