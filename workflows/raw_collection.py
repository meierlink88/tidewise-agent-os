"""Lifecycle and orchestration for the Studio-managed Raw Collection Workflow."""

from typing import Any

from agno.agent import Agent
from agno.db.base import ComponentType
from agno.registry import Registry
from agno.workflow import Condition, Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from agents.title_curator import TITLE_CURATOR_AGENT_ID, LoadedTitleCuratorAgent, load_title_curator_agent
from capabilities.collection.functions import (
    article_has_evidence,
    article_needs_review,
    article_processing_complete,
    collect_articles,
    prepare_next_article,
    publish_reviewed_article,
    save_article_review,
    validate_article_review,
)
from db import get_postgres_db

RAW_COLLECTION_WORKFLOW_ID = "raw-collection"
RAW_COLLECTION_CONTRACT_VERSION = 20
RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID = "raw-collector"


def _fail_fast_review() -> HumanReview:
    """Preserve the v2 fail-fast step contract through Agno v3 HumanReview."""
    return HumanReview(on_error=OnError.fail)


def _workflow_dependencies(
    curator: LoadedTitleCuratorAgent,
) -> dict[str, object]:
    """Serialize the pinned filter Agent provenance without branch drift."""
    return {
        "title_curator_agent_component_id": curator.agent.id,
        "title_curator_agent_config_version": curator.version,
        "title_curator_instructions_sha256": curator.instructions_sha256,
    }


def _repin_title_curator_links(links: list[dict[str, Any]], version: int) -> list[dict[str, Any]]:
    """Preserve Workflow links while moving its one curator Step to a reviewed version."""
    repinned: list[dict[str, Any]] = []
    matches = 0
    for link in links:
        normalized: dict[str, Any] = {}
        for key in ("link_kind", "link_key", "child_component_id", "child_version", "position", "meta"):
            if key in link:
                normalized[key] = link.get(key)
        if (
            normalized.get("link_kind") == "step_agent"
            and normalized.get("child_component_id") == TITLE_CURATOR_AGENT_ID
        ):
            normalized["child_version"] = version
            matches += 1
        repinned.append(normalized)
    if matches != 1:
        raise ValueError("Raw Collection must contain exactly one Title Curator Agent link")
    return repinned


def _seed_workflow(curator: Agent, *, dependencies: dict[str, object] | None = None) -> Workflow:
    """Return the code-reviewed initial Workflow graph saved to Studio once."""
    return Workflow(
        id=RAW_COLLECTION_WORKFLOW_ID,
        name="Raw Collection",
        description="Collect and deduplicate articles; review once and publish Raw Evidence and Evidence per article.",
        db=get_postgres_db(),
        dependencies=dependencies,
        metadata={"raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION},
        steps=[
            Step(
                name="collect-raw-evidence",
                executor=collect_articles,  # type: ignore[arg-type]  # Agno injects RunContext by name.
                max_retries=0,
                human_review=_fail_fast_review(),
            ),
            Loop(
                name="process-articles",
                description="Complete one article before selecting the next; skip excluded and resume frozen work.",
                max_iterations=1_000,
                end_condition=article_processing_complete,
                # Every iteration claims one article; never feed a prior article's output to its successor.
                forward_iteration_output=False,
                human_review=_fail_fast_review(),
                steps=[
                    Step(
                        name="prepare-next-article",
                        executor=prepare_next_article,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                    ),
                    Condition(
                        name="review-required",
                        evaluator=article_needs_review,
                        human_review=_fail_fast_review(),
                        steps=[
                            Step(
                                name="review-and-extract",
                                agent=curator,
                                max_retries=0,
                                human_review=_fail_fast_review(),
                            ),
                            Step(
                                name="save-article-review",
                                executor=save_article_review,  # type: ignore[arg-type]  # Agno injects RunContext.
                                max_retries=0,
                                human_review=_fail_fast_review(),
                            ),
                        ],
                    ),
                    Step(
                        name="validate-and-deduplicate",
                        executor=validate_article_review,  # type: ignore[arg-type]  # Agno injects RunContext.
                        max_retries=0,
                        human_review=_fail_fast_review(),
                        strict_input_validation=True,
                    ),
                    Condition(
                        name="publish-eligible-article",
                        evaluator=article_has_evidence,
                        human_review=_fail_fast_review(),
                        steps=[
                            Step(
                                name="publish-article-and-evidence",
                                executor=publish_reviewed_article,  # type: ignore[arg-type]  # Agno injects RunContext.
                                max_retries=0,
                                human_review=_fail_fast_review(),
                            )
                        ],
                    ),
                ],
            ),
        ],
    )


def ensure_raw_collection_workflow(registry: Registry) -> int:
    """Create the initial published Workflow once; never overwrite Studio versions."""
    db = get_postgres_db()
    component = db.get_component(RAW_COLLECTION_WORKFLOW_ID, component_type=ComponentType.WORKFLOW)
    if component is not None:
        version = component.get("current_version")
        if not isinstance(version, int):
            raise ValueError("Raw Collection has no published Studio version")
        saved = db.get_config(component_id=RAW_COLLECTION_WORKFLOW_ID, version=version)
        config = saved.get("config") if isinstance(saved, dict) else None
        if not isinstance(config, dict):
            raise ValueError("Raw Collection published Studio config is missing")
        metadata = dict(config.get("metadata") or {})
        if metadata.get("raw_collection_contract_version") == RAW_COLLECTION_CONTRACT_VERSION:
            current = Workflow.load(RAW_COLLECTION_WORKFLOW_ID, db=db, registry=registry, version=version)
            if current is None or not isinstance(current.steps, list) or not current.steps:
                raise ValueError("Raw Collection published Studio version could not be rehydrated")
            curator = load_title_curator_agent(registry)
            expected_dependencies = _workflow_dependencies(curator)
            links = db.get_links(component_id=RAW_COLLECTION_WORKFLOW_ID, version=version)
            curator_pins = [
                link.get("child_version")
                for link in links
                if link.get("link_kind") == "step_agent" and link.get("child_component_id") == TITLE_CURATOR_AGENT_ID
            ]
            if curator_pins == [curator.version] and current.dependencies == expected_dependencies:
                return version
            current.dependencies = expected_dependencies
            db.upsert_component(
                component_id=RAW_COLLECTION_WORKFLOW_ID,
                component_type=ComponentType.WORKFLOW,
                name=current.name,
                description=current.description,
                metadata=current.metadata,
            )
            refreshed = db.upsert_config(
                component_id=RAW_COLLECTION_WORKFLOW_ID,
                config=current.to_dict(),
                links=_repin_title_curator_links(links, curator.version),
                stage="published",
                notes="Refresh Raw Collection Title Curator version pin",
            )
            refreshed_version = refreshed.get("version") if isinstance(refreshed, dict) else None
            if not isinstance(refreshed_version, int):
                raise ValueError("Raw Collection Agent version refresh failed")
            return refreshed_version
        curator = load_title_curator_agent(registry)
        migrated = _seed_workflow(
            curator.agent,
            dependencies=_workflow_dependencies(curator),
        )
        migrated.id = str(config.get("id") or RAW_COLLECTION_WORKFLOW_ID)
        migrated.name = str(config.get("name") or "Raw Collection")
        migrated.metadata = {**metadata, "raw_collection_contract_version": RAW_COLLECTION_CONTRACT_VERSION}
        published = migrated.save(
            db=db,
            stage="published",
            notes=f"Raw Collection runtime contract migration {RAW_COLLECTION_CONTRACT_VERSION}",
        )
        if not isinstance(published, int):
            raise ValueError("Raw Collection runtime contract migration failed")
        return published

    curator = load_title_curator_agent(registry)
    version = _seed_workflow(
        curator.agent,
        dependencies=_workflow_dependencies(curator),
    ).save(
        db=db,
        stage="published",
        notes="Initial code-reviewed Raw Collection Workflow seed",
    )
    if not isinstance(version, int):
        raise ValueError("Raw Collection Workflow seed did not produce a published version")
    return version


def retire_collection_query_planner_agent() -> bool:
    """Soft-archive the removed Planner after its Workflow dependency is migrated away."""
    db = get_postgres_db()
    component = db.get_component(RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID, component_type=ComponentType.AGENT)
    if component is None:
        return False
    version = component.get("current_version")
    if not isinstance(version, int):
        raise ValueError("retired Collection Query Planner has no published version")
    return db.delete_component(
        RETIRED_COLLECTION_QUERY_PLANNER_AGENT_ID,
        expected_current_version=version,
        require_no_dependents=False,
    )
