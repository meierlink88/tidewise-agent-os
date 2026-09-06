"""Narrow Agno 3.0.1 compatibility for persisted, nested collection/Event steps."""

from typing import Any

from agno.workflow import Step, Workflow

from agents.event_association import bind_event_skills
from capabilities.event import STORYLINE_AGENT_IDS

_update_session_info = Workflow.update_agents_and_teams_session_info


def _bind_nested_review(workflow: Workflow, node: Any) -> None:
    if isinstance(node, Step) and node.agent is not None and node.agent.id == "title-curator":
        node.agent.workflow_id = workflow.id
    if (
        workflow.id == "event-extraction"
        and isinstance(node, Step)
        and node.agent is not None
        and node.agent.id in STORYLINE_AGENT_IDS
    ):
        node.agent.workflow_id = workflow.id
        node.agent.db = None
        bind_event_skills(node.agent)
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
