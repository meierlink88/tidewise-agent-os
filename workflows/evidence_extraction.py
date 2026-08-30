"""Lifecycle and orchestration for the Studio-managed Evidence Extraction Workflow."""

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Loop, Step, Steps, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.evidence_extractor import load_evidence_extractor_agent
from capabilities.evidence.functions import (
    evidence_extraction_complete,
    prepare_evidence_analysis,
    prepare_raw_document,
    publish_evidences,
    validate_evidence_analysis,
)
from db import get_postgres_db

EVIDENCE_EXTRACTION_WORKFLOW_ID = "evidence-extraction"
EVIDENCE_EXTRACTION_CONTRACT_VERSION = 9


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
                max_iterations=100,
                end_condition=evidence_extraction_complete,
                steps=[
                    Steps(
                        name="extract-evidences",
                        description="Read one pending document and freeze its canonical business propositions.",
                        human_review=_fail_fast_review(),
                        steps=[
                            Step(
                                name="prepare-raw-document",
                                executor=prepare_raw_document,
                                max_retries=0,
                                human_review=_fail_fast_review(),
                            ),
                            Step(
                                name="prepare-evidence-analysis",
                                executor=prepare_evidence_analysis,  # type: ignore[arg-type]  # Agno injects RunContext.
                                max_retries=0,
                                human_review=_fail_fast_review(),
                                strict_input_validation=True,
                            ),
                            Step(
                                name="analyze-raw-evidence",
                                agent=agent,
                                max_retries=0,
                                human_review=_fail_fast_review(),
                                strict_input_validation=True,
                            ),
                            Step(
                                name="validate-evidence-analysis",
                                executor=validate_evidence_analysis,  # type: ignore[arg-type]  # Agno injects RunContext.
                                max_retries=0,
                                human_review=_fail_fast_review(),
                                strict_input_validation=True,
                            ),
                        ],
                    ),
                    Steps(
                        name="publish-evidences",
                        description="Publish Raw Evidence and its complete canonical Evidence set to Data Service.",
                        human_review=_fail_fast_review(),
                        steps=[
                            Step(
                                name="publish-evidence-set",
                                executor=publish_evidences,
                                max_retries=0,
                                human_review=_fail_fast_review(),
                                strict_input_validation=True,
                            ),
                        ],
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
