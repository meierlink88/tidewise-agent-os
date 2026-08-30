"""Resumable, episode-free Company projection command line boundary."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.settings import default_model
from capabilities.company import (
    DecisionJournal,
    GraphitiCompanyTargetSelector,
    ProjectionRunManifest,
    company_snapshot_fingerprint,
    infer_companies,
    validate_decision_candidate_scope,
)
from sematica.graphiti.company import load_company_target_catalog
from sematica.graphiti.runtime import create_agentos_graphiti
from sematica.ontology import ONTOLOGY_VERSION
from sematica.projection.company import (
    TargetLabel,
    build_inferred_edges,
    build_plan,
    company_subject,
    execute_company_projection,
    inspect_company_projection_state,
    load_facts,
    preflight_canonical_targets,
    preflight_company_namespace,
    preflight_company_relation_namespace,
    preflight_formal_industry_targets,
    verify_company_projection,
)
from sematica.projection.runtime import ProjectionError, RuntimeConfig, load_config

POLICY_VERSION = "company-projection-policy/v2"
PROMPT_CONTRACT_VERSION = "company-target-selection/v2"
GRAPH_WRITE_LOCK_PATH = Path(__file__).resolve().parents[2] / "data/company-projection/.graph-write.lock"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project Data Companies and bounded existing-target relations without Graphiti episodes"
    )
    parser.add_argument("--env-file", type=Path, help="private runtime environment")
    parser.add_argument("--run-dir", type=Path, required=True, help="immutable decision checkpoint directory")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="freeze and validate the Data snapshot and canonical target catalog")
    plan.add_argument("--max-chain-candidates", type=_positive_int, default=80)
    infer = commands.add_parser("infer", help="resume model decisions without graph writes")
    infer.add_argument("--limit", type=_positive_int, help="freeze at most N new Company decisions")
    infer.add_argument("--batch-size", type=_positive_int, default=20)
    run = commands.add_parser("run", help="directly upsert frozen Company nodes and relations")
    run.add_argument(
        "--replace",
        action="store_true",
        help="after a complete write, remove stale Company projection facts and verify exact parity",
    )
    commands.add_parser("verify", help="compare graph facts with Data and every frozen decision")
    return parser


def _runtime_config(env_file: Path | None) -> RuntimeConfig:
    if env_file is not None:
        return load_config(env_file)
    aliases = {field.alias for field in RuntimeConfig.model_fields.values()}
    values = {key: value for key, value in os.environ.items() if key in aliases}
    values.setdefault("TIDEWISE_DATA_BASE_URL", os.getenv("DATA_SERVICE_BASE_URL", ""))
    values.setdefault("TIDEWISE_DATA_SERVICE_TOKEN", os.getenv("DATA_SERVICE_TOKEN", ""))
    try:
        return RuntimeConfig.model_validate(values)
    except ValidationError as exc:
        fields = sorted({str(item["loc"][0]) for item in exc.errors()})
        raise ProjectionError(
            "Company projection runtime is incomplete; pass --env-file or configure: " + ", ".join(fields)
        ) from None


def _candidate_manifest(
    facts,
    subjects,
    catalog,
    max_chain_candidates: int,
    model_id: str,  # type: ignore[no-untyped-def]
) -> ProjectionRunManifest:
    return ProjectionRunManifest(
        snapshot_id=facts.snapshot_id,
        company_snapshot_fingerprint=company_snapshot_fingerprint(subjects),
        target_catalog_fingerprint=catalog.fingerprint(),
        company_ids=[subject.company_id for subject in subjects],
        ontology_version=ONTOLOGY_VERSION,
        policy_version=POLICY_VERSION,
        model_id=model_id,
        prompt_contract_version=PROMPT_CONTRACT_VERSION,
        max_chain_candidates=max_chain_candidates,
        created_at=datetime.now(UTC),
    )


def _manifest_identity(manifest: ProjectionRunManifest) -> dict[str, object]:
    return manifest.model_dump(mode="json", exclude={"created_at"})


@contextmanager
def _graph_write_exclusive(enabled: bool) -> Iterator[None]:
    """Serialize Company graph mutation across all run directories on this AgentOS workspace."""

    if not enabled:
        yield
        return
    GRAPH_WRITE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GRAPH_WRITE_LOCK_PATH.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another Company graph projection write is active") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _open_or_validate_manifest(
    journal: DecisionJournal,
    candidate: ProjectionRunManifest,
    *,
    create: bool,
) -> ProjectionRunManifest:
    if not journal.manifest_path.is_file():
        if not create:
            raise ProjectionError("Company projection plan is missing; run plan first")
        journal.open_or_create(candidate)
        return candidate
    existing = journal.manifest()
    if _manifest_identity(existing) != _manifest_identity(candidate):
        raise ProjectionError("Company projection inputs differ from the frozen run manifest")
    return existing


def _decision_summary(decisions, total: int) -> dict[str, object]:  # type: ignore[no-untyped-def]
    statuses = Counter(decision.status.value for decision in decisions)
    return {
        "decisions_frozen": len(decisions),
        "decisions_total": total,
        "decisions_complete": len(decisions) == total,
        "decision_statuses": dict(sorted(statuses.items())),
        "inferred_industry_relations": sum(len(item.industry.accepted_targets) for item in decisions),
        "inferred_chain_node_relations": sum(len(item.chain_node.accepted_targets) for item in decisions),
    }


async def _main(args: argparse.Namespace) -> dict[str, object]:
    config = _runtime_config(args.env_file)
    journal = DecisionJournal(args.run_dir)
    with journal.exclusive(), _graph_write_exclusive(args.command == "run"):
        facts = await load_facts(config)
        plan = build_plan(facts)
        subjects = [company_subject(company, index) for index, company in enumerate(facts.companies)]
        model = default_model()
        model_id = str(model.id)
        graphiti = create_agentos_graphiti(model, config)
        try:
            catalog = await load_company_target_catalog(graphiti)
            await preflight_formal_industry_targets(graphiti, plan)
            await preflight_company_namespace(graphiti, plan)
            await preflight_company_relation_namespace(graphiti, plan.formal_industry_edges)
            if args.command == "plan":
                max_chain_candidates = args.max_chain_candidates
            else:
                if not journal.manifest_path.is_file():
                    raise ProjectionError("Company projection plan is missing; run plan first")
                max_chain_candidates = journal.manifest().max_chain_candidates
            candidate = _candidate_manifest(facts, subjects, catalog, max_chain_candidates, model_id)
            manifest = _open_or_validate_manifest(journal, candidate, create=args.command == "plan")
            common = {
                **plan.summary(),
                "run_dir": str(journal.root),
                "company_snapshot_fingerprint": manifest.company_snapshot_fingerprint,
                "target_catalog_fingerprint": manifest.target_catalog_fingerprint,
                "canonical_industries": len(catalog.industries),
                "canonical_industry_chains": len(catalog.industry_chains),
                "canonical_chain_nodes": len(catalog.chain_nodes),
                "write_mode": "direct-entity-node-edge-bulk-no-episode",
            }
            if args.command == "plan":
                return {**common, "preflight_validated": True, **_decision_summary(journal.completed(), len(subjects))}
            if args.command == "infer":
                decisions = await infer_companies(
                    subjects,
                    catalog,
                    manifest,
                    journal,
                    GraphitiCompanyTargetSelector(graphiti),
                    industry_batch_size=args.batch_size,
                    max_chain_candidates=manifest.max_chain_candidates,
                    max_new_decisions=args.limit,
                )
                return {**common, **_decision_summary(decisions, len(subjects)), "graph_writes": 0}

            try:
                decisions = journal.assert_complete()
            except FileNotFoundError:
                raise ProjectionError("Company inference decisions are incomplete; resume infer first") from None
            try:
                catalog_fingerprint = catalog.fingerprint()
                for decision in decisions:
                    validate_decision_candidate_scope(
                        decision,
                        catalog,
                        manifest,
                        catalog_fingerprint=catalog_fingerprint,
                    )
            except ValueError as exc:
                raise ProjectionError(f"frozen Company candidate audit is invalid: {exc}") from None
            target_labels: dict[str, TargetLabel] = {target_id: "Industry" for target_id in plan.formal_industry_ids}
            for decision in decisions:
                target_labels.update({item.target_id: "Industry" for item in decision.industry.accepted_targets})
                target_labels.update({item.target_id: "ChainNode" for item in decision.chain_node.accepted_targets})
            targets = await preflight_canonical_targets(graphiti, target_labels)
            inferred_edges = build_inferred_edges(facts, decisions, targets)
            await preflight_company_relation_namespace(
                graphiti,
                [*plan.formal_industry_edges, *inferred_edges],
            )
            if args.command == "verify":
                state = await inspect_company_projection_state(graphiti)
                result = verify_company_projection(
                    plan,
                    inferred_edges,
                    state,
                    embedding_dimension=config.graphiti_embedding_dim,
                )
                return {**common, **_decision_summary(decisions, len(subjects)), **result}

            result = await execute_company_projection(
                graphiti,
                facts,
                plan,
                decisions,
                manifest,
                embedding_dimension=config.graphiti_embedding_dim,
                replace=args.replace,
                progress=lambda completed, total: print(
                    f"embedded {completed}/{total}",
                    file=sys.stderr,
                    flush=True,
                ),
            )
            return {
                **common,
                **_decision_summary(decisions, len(subjects)),
                **result,
                "canonical_targets_preserved": True,
            }
        finally:
            await graphiti.close()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_main(args))
    except (ProjectionError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "interrupted"}), file=sys.stderr)
        return 130
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
