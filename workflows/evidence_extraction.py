"""Lifecycle and orchestration for the Studio-managed Evidence Extraction Workflow."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.evidence_extractor import load_evidence_extractor_agent
from capabilities.evidence.functions import (
    curate_evidence,
    evidence_extraction_complete,
    prepare_evidence,
    publish_evidence,
)
from db import get_postgres_db

EVIDENCE_EXTRACTION_WORKFLOW_ID = "evidence-extraction"
EVIDENCE_EXTRACTION_CONTRACT_VERSION = 11
EVIDENCE_EXTRACTION_BATCH_LIMIT = 20


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
            return version
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
