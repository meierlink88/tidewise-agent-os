"""Lifecycle and orchestration for the Studio-managed Event Extraction Workflow."""

from typing import Any

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.event_extractor import load_event_extractor_agent
from agents.event_identity import load_event_identity_agent
from agents.event_signal_analyst import load_event_signal_analyst_agent
from capabilities.event import (
    EVENT_AGENT_IDS,
    EVENT_EXTRACTOR_AGENT_ID,
    EVENT_IDENTITY_AGENT_ID,
    EVENT_SIGNAL_ANALYST_AGENT_ID,
    EventAgentVersions,
)
from capabilities.event.functions import (
    analyze_signals,
    event_extraction_complete,
    extract_events,
    publish_events,
    publish_signals,
    resolve_events,
)
from db import get_postgres_db

EVENT_EXTRACTION_WORKFLOW_ID = "event-extraction"
EVENT_EXTRACTION_CONTRACT_VERSION = 13
EVENT_EXTRACTION_BATCH_LIMIT = 50
EVENT_EXTRACTION_PUBLICATION_POLICY = "code_managed_exact_agent_links.v1"
_AGENT_LINK_BINDINGS = (
    ("event-extract", EVENT_EXTRACTOR_AGENT_ID, 0),
    ("event-resolve", EVENT_IDENTITY_AGENT_ID, 1),
    ("event-signal-analyze", EVENT_SIGNAL_ANALYST_AGENT_ID, 3),
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


def _seed_workflow(
    extractor: Agent,
    identity: Agent,
    signal_analyst: Agent,
    *,
    agent_versions: dict[str, int],
) -> Workflow:
    """Build one visible batch Loop containing five direct business Function Steps."""

    if {str(agent.id) for agent in (extractor, identity, signal_analyst)} != EVENT_AGENT_IDS:
        raise ValueError("Event Extraction requires exactly its three Studio Agents")
    try:
        pinned_versions = EventAgentVersions.model_validate(agent_versions).as_mapping()
    except ValueError as exc:
        raise ValueError("Event Extraction requires complete positive exact Agent versions") from exc

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
            "event_extraction_publication_policy": EVENT_EXTRACTION_PUBLICATION_POLICY,
            "event_agent_versions": pinned_versions,
        },
        steps=[
            Loop(
                name="Process Event Evidence batches",
                description="Process frozen Evidence batches until the queue is drained or the safety cap is reached.",
                max_iterations=EVENT_EXTRACTION_BATCH_LIMIT,
                end_condition=event_extraction_complete,
                human_review=_fail_fast_review(),
                steps=[
                    _function_step("Extract Events", "event-extract", extract_events),
                    _function_step("Resolve Events", "event-resolve", resolve_events),
                    _function_step("Publish Events", "event-publish", publish_events),
                    _function_step("Analyze Signals", "event-signal-analyze", analyze_signals),
                    _function_step("Publish Signals", "event-signal-publish", publish_signals),
                ],
            )
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

    try:
        pinned_versions = EventAgentVersions.model_validate(agent_versions).as_mapping()
    except ValueError as exc:
        raise ValueError("Event Extraction requires complete positive exact Agent versions") from exc
    links = [
        {
            "link_kind": "step_agent",
            "link_key": step_id,
            "child_component_id": agent_id,
            "child_version": pinned_versions[agent_id],
            "position": position,
        }
        for step_id, agent_id, position in _AGENT_LINK_BINDINGS
    ]
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


def _validate_published_agent_pins(db: Any, version: int, metadata: dict[str, Any]) -> dict[str, int]:
    if metadata.get("event_extraction_publication_policy") != EVENT_EXTRACTION_PUBLICATION_POLICY:
        raise ValueError("Event Extraction must use its code-managed exact-link publication policy")
    pinned_metadata = metadata.get("event_agent_versions")
    if not isinstance(pinned_metadata, dict) or set(pinned_metadata) != EVENT_AGENT_IDS:
        raise ValueError("Event Extraction published Agent version metadata is incomplete")
    try:
        pinned_versions = EventAgentVersions.model_validate(pinned_metadata).as_mapping()
    except ValueError as exc:
        raise ValueError("Event Extraction published Agent version metadata is invalid") from exc
    links = db.get_links(component_id=EVENT_EXTRACTION_WORKFLOW_ID, version=version)
    linked_bindings = sorted(
        (
            str(link.get("link_key")),
            str(link.get("child_component_id")),
            link.get("child_version"),
            link.get("position"),
        )
        for link in links
        if link.get("link_kind") == "step_agent"
    )
    expected_bindings = sorted(
        (step_id, agent_id, pinned_versions[agent_id], position) for step_id, agent_id, position in _AGENT_LINK_BINDINGS
    )
    if len(links) != len(_AGENT_LINK_BINDINGS) or linked_bindings != expected_bindings:
        raise ValueError("Event Extraction published Workflow does not pin all exact Agent versions")
    return pinned_versions


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
            pinned_versions = _validate_published_agent_pins(db, version, metadata)
            extractor, identity, analyst, versions = _loaded_agents(registry)
            if pinned_versions == versions:
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

            refreshed = _seed_workflow(extractor, identity, analyst, agent_versions=versions)
            refreshed.id = str(config.get("id") or EVENT_EXTRACTION_WORKFLOW_ID)
            refreshed.name = str(config.get("name") or "Event Extraction")
            refreshed.description = str(config.get("description") or refreshed.description)
            refreshed.metadata = {
                **metadata,
                "event_extraction_publication_policy": EVENT_EXTRACTION_PUBLICATION_POLICY,
                "event_agent_versions": dict(sorted(versions.items())),
            }
            return _publish_pinned_workflow(
                refreshed,
                agent_versions=versions,
                notes="Refresh Event Extraction exact Agent version pins",
            )

        extractor, identity, analyst, versions = _loaded_agents(registry)
        migrated = _seed_workflow(extractor, identity, analyst, agent_versions=versions)
        migrated.id = str(config.get("id") or EVENT_EXTRACTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Event Extraction")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {
            **metadata,
            "event_extraction_contract_version": EVENT_EXTRACTION_CONTRACT_VERSION,
            "event_extraction_publication_policy": EVENT_EXTRACTION_PUBLICATION_POLICY,
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
