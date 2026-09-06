"""Lifecycle and orchestration for the Studio-managed Evidence Extraction Workflow."""

from typing import Any

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.evidence_extractor import EVIDENCE_EXTRACTOR_AGENT_ID, load_evidence_extractor_agent
from capabilities.evidence.functions import (
    curate_evidence,
    evidence_extraction_complete,
    prepare_evidence,
    publish_evidence,
)
from db import get_postgres_db

EVIDENCE_EXTRACTION_WORKFLOW_ID = "evidence-extraction"
EVIDENCE_EXTRACTION_CONTRACT_VERSION = 13
EVIDENCE_EXTRACTION_BATCH_LIMIT = 20


def _repin_evidence_extractor_links(links: list[dict[str, Any]], version: int) -> list[dict[str, Any]]:
    """Preserve Workflow links while moving its one extractor Step to a reviewed version."""
    repinned: list[dict[str, Any]] = []
    matches = 0
    for link in links:
        normalized: dict[str, Any] = {}
        for key in ("link_kind", "link_key", "child_component_id", "child_version", "position", "meta"):
            if key in link:
                normalized[key] = link.get(key)
        if (
            normalized.get("link_kind") == "step_agent"
            and normalized.get("child_component_id") == EVIDENCE_EXTRACTOR_AGENT_ID
        ):
            normalized["child_version"] = version
            matches += 1
        repinned.append(normalized)
    if matches != 1:
        raise ValueError("Evidence Extraction must contain exactly one Evidence Extractor Agent link")
    return repinned


def _fail_fast_review() -> HumanReview:
    """Preserve the v2 fail-fast step contract through Agno v3 HumanReview."""
    return HumanReview(on_error=OnError.fail)


def _seed_workflow(agent: Agent) -> Workflow:
    """Return the code-reviewed initial Workflow graph saved to Studio once."""
    return Workflow(
        id=EVIDENCE_EXTRACTION_WORKFLOW_ID,
        name="Evidence Extraction",
        description="Incrementally extracts and publishes Raw Evidence and atomic Evidence.",
        db=get_postgres_db(),
        dependencies={},
        metadata={"evidence_extraction_contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION},
        steps=[
            Loop(
                name="process-unpublished-raw-documents",
                description="Process indexed Raw documents until no work remains or the safety cap is reached.",
                max_iterations=EVIDENCE_EXTRACTION_BATCH_LIMIT,
                end_condition=evidence_extraction_complete,
                steps=[
                    Step(
                        name="prepare-evidence",
                        executor=prepare_evidence,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                    Step(
                        name="extract-evidence",
                        agent=agent,
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                    Step(
                        name="curate-evidence",
                        executor=curate_evidence,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                    Step(
                        name="publish-evidence",
                        executor=publish_evidence,
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                ],
            )
        ],
    )


def ensure_evidence_extraction_workflow(registry: Registry) -> int:
    """Create the initial published Workflow once; never overwrite Studio versions."""
    db = get_postgres_db()
    component = db.get_component(EVIDENCE_EXTRACTION_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Evidence Extraction has no published Studio version")
        saved = db.get_config(component_id=EVIDENCE_EXTRACTION_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Evidence Extraction published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("evidence_extraction_contract_version") == EVIDENCE_EXTRACTION_CONTRACT_VERSION:
            current = Workflow.load(EVIDENCE_EXTRACTION_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Evidence Extraction published Studio version could not be rehydrated")
            agent_component = db.get_component(EVIDENCE_EXTRACTOR_AGENT_ID, component_type=ComponentType.AGENT)
            agent_version = agent_component.get("current_version") if agent_component is not None else None
            if not isinstance(agent_version, int):
                raise ValueError("Evidence Extractor has no published Studio version")
            links = db.get_links(component_id=EVIDENCE_EXTRACTION_WORKFLOW_ID, version=version)
            extractor_pins = [
                link.get("child_version")
                for link in links
                if link.get("link_kind") == "step_agent"
                and link.get("child_component_id") == EVIDENCE_EXTRACTOR_AGENT_ID
            ]
            if extractor_pins == [agent_version]:
                return version
            db.upsert_component(
                component_id=EVIDENCE_EXTRACTION_WORKFLOW_ID,
                component_type=ComponentType.WORKFLOW,
                name=current.name,
                description=current.description,
                metadata=current.metadata,
            )
            refreshed = db.upsert_config(
                component_id=EVIDENCE_EXTRACTION_WORKFLOW_ID,
                config=current.to_dict(),
                links=_repin_evidence_extractor_links(links, agent_version),
                stage="published",
                notes="Refresh Evidence Extraction Agent version pin",
            )
            refreshed_version = refreshed.get("version") if isinstance(refreshed, dict) else None
            if not isinstance(refreshed_version, int):
                raise ValueError("Evidence Extraction Agent version refresh failed")
            return refreshed_version
        agent = load_evidence_extractor_agent(registry)
        migrated = _seed_workflow(agent)
        migrated.id = str(config.get("id") or EVIDENCE_EXTRACTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Evidence Extraction")
        migrated.description = str(config.get("description") or migrated.description)
        migrated.metadata = {
            **metadata,
            "evidence_extraction_contract_version": EVIDENCE_EXTRACTION_CONTRACT_VERSION,
        }
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Evidence Extraction runtime contract migration {EVIDENCE_EXTRACTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Evidence Extraction runtime contract migration failed")
        return published

    agent = load_evidence_extractor_agent(registry)
    version = _seed_workflow(agent).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Evidence Extraction Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Evidence Extraction Workflow seed did not produce a published version")
    return version
