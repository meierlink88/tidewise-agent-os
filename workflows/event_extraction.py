"""Lifecycle and orchestration for the Studio-managed Event Extraction Workflow."""

from typing import Any

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Condition, Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError
from agno.workflow.workflow import derive_step_links

from agents.event_extractor import EVENT_EXTRACTOR_AGENT_ID, load_event_extractor_agent
from agents.event_identity import EVENT_IDENTITY_AGENT_ID, load_event_identity_agent
from agents.event_signal_analyst import EVENT_SIGNAL_ANALYST_AGENT_ID, load_event_signal_analyst_agent
from capabilities.event.functions import (
    event_extraction_required,
    event_resolution_complete,
    freeze_event_extraction,
    has_pending_event_resolution,
    has_pending_signal_analysis,
    persist_event_resolution,
    persist_signal_task,
    prepare_event_extraction,
    prepare_event_resolution,
    prepare_signal_task,
    publish_events,
    publish_signals,
    signal_analysis_complete,
)
from db import get_postgres_db

EVENT_EXTRACTION_WORKFLOW_ID = "event-extraction"
EVENT_EXTRACTION_CONTRACT_VERSION = 8
EVENT_EXTRACTION_BATCH_LIMIT = 50
EVENT_SIGNAL_TASK_LIMIT = EVENT_EXTRACTION_BATCH_LIMIT * 2
EVENT_AGENT_IDS = frozenset(
    {
        EVENT_EXTRACTOR_AGENT_ID,
        EVENT_IDENTITY_AGENT_ID,
        EVENT_SIGNAL_ANALYST_AGENT_ID,
    }
)


def _fail_fast_review() -> HumanReview:
    return HumanReview(on_error=OnError.fail)


def _function_step(name: str, step_id: str, executor: Any) -> Step:
    return Step(
        name=name,
        step_id=step_id,
        executor=executor,
        max_retries=0,
        human_review=_fail_fast_review(),
        strict_input_validation=True,
    )


def _agent_step(name: str, step_id: str, agent: Agent) -> Step:
    return Step(
        name=name,
        step_id=step_id,
        agent=agent,
        max_retries=0,
        skip_on_failure=True,
        human_review=_fail_fast_review(),
        strict_input_validation=True,
    )


def _seed_workflow(
    extractor: Agent,
    identity: Agent,
    signal_analyst: Agent,
    *,
    agent_versions: dict[str, int],
) -> Workflow:
    """Build five business phases around three direct Studio Agent seams."""

    return Workflow(
        id=EVENT_EXTRACTION_WORKFLOW_ID,
        name="Event Extraction",
        description=(
            "Extracts frozen Evidence, resolves formal Event identity, publishes Data Events and native "
            "Graphiti Episodes, then validates and projects direct Signal Facts."
        ),
        db=get_postgres_db(),
        dependencies={},
        metadata={
            "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
            "event_agent_versions": dict(sorted(agent_versions.items())),
        },
        steps=[
            Condition(
                name="Extract Events",
                evaluator=True,
                human_review=_fail_fast_review(),
                steps=[
                    _function_step("Prepare Event extraction", "event-extract-prepare", prepare_event_extraction),
                    Condition(
                        name="Run Event Extractor when draft is absent",
                        evaluator=event_extraction_required,  # type: ignore[arg-type]  # Agno injects RunContext.
                        human_review=_fail_fast_review(),
                        steps=[
                            _agent_step("Extract atomic Events", "event-extract-agent", extractor),
                            _function_step("Freeze Event extraction", "event-extract-freeze", freeze_event_extraction),
                        ],
                    ),
                ],
            ),
            Condition(
                name="Resolve Events",
                evaluator=has_pending_event_resolution,  # type: ignore[arg-type]  # Agno injects RunContext.
                human_review=_fail_fast_review(),
                steps=[
                    Loop(
                        name="Resolve frozen Event Candidates",
                        max_iterations=EVENT_EXTRACTION_BATCH_LIMIT,
                        end_condition=event_resolution_complete,
                        human_review=_fail_fast_review(),
                        steps=[
                            _function_step(
                                "Prepare Event identity",
                                "event-identity-prepare",
                                prepare_event_resolution,
                            ),
                            _agent_step("Resolve Event identity", "event-identity-agent", identity),
                            _function_step("Freeze Event identity", "event-identity-freeze", persist_event_resolution),
                        ],
                    )
                ],
            ),
            _function_step("Publish Events", "event-publish", publish_events),
            Condition(
                name="Analyze Signals",
                evaluator=has_pending_signal_analysis,  # type: ignore[arg-type]  # Agno injects RunContext.
                human_review=_fail_fast_review(),
                steps=[
                    Loop(
                        name="Classify Events and validate direct Signals",
                        max_iterations=EVENT_SIGNAL_TASK_LIMIT,
                        end_condition=signal_analysis_complete,
                        human_review=_fail_fast_review(),
                        steps=[
                            _function_step("Prepare Signal task", "event-signal-prepare", prepare_signal_task),
                            _agent_step("Analyze direct Signals", "event-signal-agent", signal_analyst),
                            _function_step("Freeze Signal analysis", "event-signal-freeze", persist_signal_task),
                        ],
                    )
                ],
            ),
            _function_step("Publish Signals", "event-signal-publish", publish_signals),
        ],
    )


def _publish_pinned_workflow(
    workflow: Workflow,
    *,
    agent_versions: dict[str, int],
    notes: str,
) -> int:
    """Publish without re-saving Agents, pinning exactly the versions already reviewed."""

    if workflow.id is None:
        raise ValueError("Event Extraction Workflow requires a stable ID")

    def pin_child(link: dict[str, Any]) -> dict[str, Any]:
        child_id = link.get("child_component_id")
        version = agent_versions.get(str(child_id))
        if version is None:
            raise ValueError(f"Event Extraction has no exact version for child Agent {child_id}")
        return {**link, "child_version": version}

    links = derive_step_links(
        workflow.steps,
        pin_child=pin_child,
        workflow_id=workflow.id,
    )
    pinned_ids = {str(link.get("child_component_id")) for link in links if link.get("link_kind") == "step_agent"}
    if pinned_ids != EVENT_AGENT_IDS:
        raise ValueError("Event Extraction must pin exactly its three Studio Agents")
    db = get_postgres_db()
    db.upsert_component(
        component_id=workflow.id,
        component_type=ComponentType.WORKFLOW,
        name=workflow.name,
        description=workflow.description,
        metadata=workflow.metadata,
    )
    saved = db.upsert_config(
        component_id=workflow.id,
        config=workflow.to_dict(),
        links=links,
        stage="published",
        notes=notes,
    )
    version = saved.get("version") if isinstance(saved, dict) else None
    if not isinstance(version, int):
        raise ValueError("Event Extraction Workflow publication did not produce a version")
    return version


def _validate_published_agent_pins(db: Any, version: int, metadata: dict[str, Any]) -> None:
    pinned_metadata = metadata.get("event_agent_versions")
    if not isinstance(pinned_metadata, dict) or set(pinned_metadata) != EVENT_AGENT_IDS:
        raise ValueError("Event Extraction published Agent version metadata is incomplete")
    if any(not isinstance(value, int) for value in pinned_metadata.values()):
        raise ValueError("Event Extraction published Agent version metadata is invalid")
    links = db.get_links(component_id=EVENT_EXTRACTION_WORKFLOW_ID, version=version)
    linked_versions = {
        str(link.get("child_component_id")): link.get("child_version")
        for link in links
        if link.get("link_kind") == "step_agent"
    }
    if linked_versions != pinned_metadata:
        raise ValueError("Event Extraction published Workflow does not pin all exact Agent versions")


def _loaded_agents(registry: Registry) -> tuple[Agent, Agent, Agent, dict[str, int]]:
    extractor = load_event_extractor_agent(registry)
    identity = load_event_identity_agent(registry)
    analyst = load_event_signal_analyst_agent(registry)
    versions = {
        str(extractor.agent.id): extractor.version,
        str(identity.agent.id): identity.version,
        str(analyst.agent.id): analyst.version,
    }
    return extractor.agent, identity.agent, analyst.agent, versions


def ensure_event_extraction_workflow(registry: Registry) -> int:
    """Seed or contract-migrate the Workflow while preserving every exact child version."""

    db = get_postgres_db()
    component = db.get_component(EVENT_EXTRACTION_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Event Extraction has no published Studio version")
        saved = db.get_config(component_id=EVENT_EXTRACTION_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Event Extraction published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("event_extraction_contract_version") == EVENT_EXTRACTION_CONTRACT_VERSION:
            _validate_published_agent_pins(db, version, metadata)
            current = Workflow.load(
                EVENT_EXTRACTION_WORKFLOW_ID,
                db=db,
                registry=registry,
                version=version,
                strict=True,
                published_only=True,
            )
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Event Extraction published Studio version could not be rehydrated")
            return version

        extractor, identity, analyst, versions = _loaded_agents(registry)
        migrated = _seed_workflow(extractor, identity, analyst, agent_versions=versions)
        migrated.id = str(config.get("id") or EVENT_EXTRACTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Event Extraction")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {
            **metadata,
            "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
            "event_agent_versions": dict(sorted(versions.items())),
        }
        return _publish_pinned_workflow(
            migrated,
            agent_versions=versions,
            notes=f"Event Extraction runtime contract migration {EVENT_EXTRACTION_CONTRACT_VERSION}",
        )

    extractor, identity, analyst, versions = _loaded_agents(registry)
    return _publish_pinned_workflow(
        _seed_workflow(extractor, identity, analyst, agent_versions=versions),
        agent_versions=versions,
        notes="Initial code-reviewed Event Extraction Workflow seed",
    )
