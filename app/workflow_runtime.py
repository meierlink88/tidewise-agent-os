"""Narrow Agno 3.0.1 compatibility for persisted, nested collection/Event steps."""

from types import MethodType
from typing import Any

from agno.exceptions import RunCancelledException
from agno.run.agent import RunCancelledEvent, RunErrorEvent
from agno.workflow import Step, StepInput, StepOutput, Workflow

from agents.event_association import bind_event_skills
from app.event_batch_runtime import bind_batch_step
from app.event_step_runtime import bind_event_step
from capabilities.collection.functions import ArticleFailureRecordingError, article_review_gate, fail_current_article
from capabilities.event import STORYLINE_AGENT_IDS

_update_session_info = Workflow.update_agents_and_teams_session_info
_review_execute = Step.execute
_review_execute_stream = Step.execute_stream
_review_aexecute = Step.aexecute
_review_aexecute_stream = Step.aexecute_stream


def _settle_review(output: StepOutput, context: Any) -> StepOutput:
    skipped = article_review_gate(context)
    if skipped is not None:
        return skipped
    if not output.success:
        return fail_current_article(context, "Evidence Reviewer", RuntimeError(output.error or "Agent step failed"))
    return output


def _guarded_execute(step: Step, step_input: StepInput, **kwargs: Any) -> StepOutput:
    context = kwargs["run_context"]
    skipped = article_review_gate(context)
    if skipped is not None:
        return skipped
    try:
        output = _review_execute(step, step_input, **kwargs)
    except (RunCancelledException, ArticleFailureRecordingError):
        raise
    except Exception as exc:
        return fail_current_article(context, "Evidence Reviewer", exc)
    return _settle_review(output, context)


async def _guarded_aexecute(step: Step, step_input: StepInput, **kwargs: Any) -> StepOutput:
    context = kwargs["run_context"]
    skipped = article_review_gate(context)
    if skipped is not None:
        return skipped
    try:
        output = await _review_aexecute(step, step_input, **kwargs)
    except (RunCancelledException, ArticleFailureRecordingError):
        raise
    except Exception as exc:
        return fail_current_article(context, "Evidence Reviewer", exc)
    return _settle_review(output, context)


def _guarded_execute_stream(step: Step, step_input: StepInput, **kwargs: Any) -> Any:
    context = kwargs["run_context"]
    skipped = article_review_gate(context)
    if skipped is not None:
        yield skipped
        return
    try:
        for event in _review_execute_stream(step, step_input, **kwargs):
            if isinstance(event, RunCancelledEvent):
                raise RunCancelledException(event.reason or "Cancelled")
            if isinstance(event, RunErrorEvent):
                fail_current_article(context, "Evidence Reviewer", RuntimeError(event.content or "Agent error"))
            yield _settle_review(event, context) if isinstance(event, StepOutput) else event
    except (RunCancelledException, ArticleFailureRecordingError):
        raise
    except Exception as exc:
        if article_review_gate(context) is not None:
            raise  # Never hide an error in failure recording itself.
        yield fail_current_article(context, "Evidence Reviewer", exc)


async def _guarded_aexecute_stream(step: Step, step_input: StepInput, **kwargs: Any) -> Any:
    context = kwargs["run_context"]
    skipped = article_review_gate(context)
    if skipped is not None:
        yield skipped
        return
    try:
        async for event in _review_aexecute_stream(step, step_input, **kwargs):
            if isinstance(event, RunCancelledEvent):
                raise RunCancelledException(event.reason or "Cancelled")
            if isinstance(event, RunErrorEvent):
                fail_current_article(context, "Evidence Reviewer", RuntimeError(event.content or "Agent error"))
            yield _settle_review(event, context) if isinstance(event, StepOutput) else event
    except (RunCancelledException, ArticleFailureRecordingError):
        raise
    except Exception as exc:
        if article_review_gate(context) is not None:
            raise
        yield fail_current_article(context, "Evidence Reviewer", exc)


def _bind_nested_review(workflow: Workflow, node: Any) -> None:
    if isinstance(node, Step) and node.agent is not None and node.agent.id == "title-curator":
        node.agent.workflow_id = workflow.id
        if (workflow.metadata or {}).get("raw_collection_contract_version", 0) >= 23:
            # Agno has no persisted skip predicate on Step. Rebind the execution boundary
            # after Studio load; business decisions and durable error records remain Functions.
            node.execute = MethodType(_guarded_execute, node)  # type: ignore[method-assign]
            node.execute_stream = MethodType(_guarded_execute_stream, node)  # type: ignore[method-assign]
            node.aexecute = MethodType(_guarded_aexecute, node)  # type: ignore[method-assign]
            node.aexecute_stream = MethodType(_guarded_aexecute_stream, node)  # type: ignore[method-assign]
    if (
        workflow.id == "event-extraction"
        and isinstance(node, Step)
        and node.agent is not None
        and node.agent.id in STORYLINE_AGENT_IDS
    ):
        node.agent.workflow_id = workflow.id
        node.agent.db = None
        bind_event_skills(node.agent)
        if (workflow.metadata or {}).get("event_extraction_contract_version", 0) >= 16:
            bind_batch_step(node)
        elif (workflow.metadata or {}).get("event_extraction_contract_version", 0) >= 15:
            bind_event_step(node)
    for attribute in ("steps", "else_steps", "choices"):
        children = getattr(node, attribute, None)
        if isinstance(children, list):
            for child in children:
                _bind_nested_review(workflow, child)


def _update_raw_collection_session_info(workflow: Workflow) -> None:
    _update_session_info(workflow)
    if workflow.id in {"raw-collection", "event-extraction"}:
        _bind_nested_review(workflow, workflow)


def install_raw_collection_session_compatibility() -> None:
    """Fill Agno's explicit nested-primitive TODO without editing installed code.

    The stock method sets workflow_id only for top-level Steps. A DB-loaded
    Agent inside Loop/Condition otherwise writes an AgentSession under the
    parent's session_id. Native Agent storage already skips session writes
    when workflow_id is bound. Scope this shim to Raw Collection's Reviewer
    and the four direct Event Agents. Rebind code-owned Event Skills omitted
    by Agent serialization, without changing pinned prompts or model versions.
    Remove it once the pinned Agno version handles nested primitives natively.
    """
    Workflow.update_agents_and_teams_session_info = _update_raw_collection_session_info  # type: ignore[method-assign]
