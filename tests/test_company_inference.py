"""Contract tests for bounded Company graph inference."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from capabilities.company import (
    CandidateChoice,
    CanonicalChainNode,
    CanonicalIndustry,
    CanonicalIndustryChain,
    ChainMembership,
    CompanyInferenceDecision,
    CompanySubject,
    Confidence,
    DecisionJournal,
    DecisionStatus,
    IndustryChainMapping,
    ModelSelectionItem,
    ModelSelectionResponse,
    ProjectionRunManifest,
    TargetCatalog,
    build_chain_node_candidates,
    company_snapshot_fingerprint,
    finalize_company_decision,
    industry_candidates_for_roots,
    industry_root_candidates,
    infer_companies,
    partition_candidate_subjects,
    validate_decision_candidate_scope,
    validate_model_response,
)

NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)
COM1 = "COM00000000-0000-4000-8000-000000000001"
COM2 = "COM00000000-0000-4000-8000-000000000002"
IND1 = "IND00000000-0000-4000-8000-000000000001"
IND2 = "IND00000000-0000-4000-8000-000000000002"
ICH1 = "ICH00000000-0000-4000-8000-000000000001"
CND1 = "CND00000000-0000-4000-8000-000000000001"
CND2 = "CND00000000-0000-4000-8000-000000000002"


def subject(company_id: str = COM1, input_index: int = 0) -> CompanySubject:
    return CompanySubject(
        input_index=input_index,
        company_id=company_id,
        code=f"TEST{input_index}",
        name="测试半导体股份有限公司",
        name_en=None,
        legal_name="测试半导体股份有限公司",
        aliases=[],
        registration_country_id=None,
        strategic_positioning=None,
        description=None,
        source_updated_at=NOW,
    )


def catalog() -> TargetCatalog:
    return TargetCatalog(
        industries=[
            CanonicalIndustry(industry_id=IND1, name="半导体", definition="半导体研发与制造", parent_id=None),
            CanonicalIndustry(industry_id=IND2, name="集成电路制造", definition="晶圆与芯片制造", parent_id=IND1),
        ],
        industry_chains=[CanonicalIndustryChain(industry_chain_id=ICH1, name="集成电路产业链")],
        chain_nodes=[
            CanonicalChainNode(chain_node_id=CND1, name="晶圆制造", definition="晶圆制造环节"),
            CanonicalChainNode(chain_node_id=CND2, name="封装测试", definition="芯片封装与测试环节"),
        ],
        industry_chain_mappings=[IndustryChainMapping(industry_chain_id=ICH1, industry_id=IND2)],
        chain_memberships=[
            ChainMembership(industry_chain_id=ICH1, chain_node_id=CND1),
            ChainMembership(industry_chain_id=ICH1, chain_node_id=CND2),
        ],
    )


class TargetCatalogTest(unittest.TestCase):
    def test_chain_prompt_partition_never_splits_a_company_candidate_set(self) -> None:
        subjects = [subject(COM1, 0), subject(COM2, 1)]
        candidate = CandidateChoice(key="N1", target_id=CND1, name="晶圆制造", definition="定义")
        candidates = {0: [candidate] * 200, 1: [candidate] * 200}

        batches = partition_candidate_subjects(subjects, candidates, candidate_budget=300)

        self.assertEqual([[item.input_index for item in batch] for batch in batches], [[0], [1]])

    def test_industry_selection_is_partitioned_by_existing_hierarchy(self) -> None:
        roots = industry_root_candidates(catalog())
        descendants = industry_candidates_for_roots(catalog(), [IND1])

        self.assertEqual([candidate.target_id for candidate in roots], [IND1])
        self.assertEqual([candidate.target_id for candidate in descendants], [IND1, IND2])
        self.assertEqual([candidate.key for candidate in descendants], ["I1", "I2"])

    def test_industry_partition_rejects_a_non_root_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "not a root Industry"):
            industry_candidates_for_roots(catalog(), [IND2])

    def test_chain_candidates_are_only_reached_through_existing_topology(self) -> None:
        candidates = build_chain_node_candidates(catalog(), [IND1], max_candidates=10)

        self.assertEqual([candidate.target_id for candidate in candidates], [CND1, CND2])
        self.assertEqual(candidates[0].source_industry_ids, [IND2])
        self.assertEqual(candidates[0].industry_chain_ids, [ICH1])

    def test_unknown_topology_endpoint_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unknown ChainNode"):
            TargetCatalog(
                industries=[CanonicalIndustry(industry_id=IND1, name="半导体", definition="定义")],
                industry_chains=[CanonicalIndustryChain(industry_chain_id=ICH1, name="集成电路产业链")],
                chain_nodes=[],
                industry_chain_mappings=[IndustryChainMapping(industry_chain_id=ICH1, industry_id=IND1)],
                chain_memberships=[ChainMembership(industry_chain_id=ICH1, chain_node_id=CND1)],
            )

    def test_candidate_limit_is_a_hard_positive_bound(self) -> None:
        with self.assertRaises(ValueError):
            build_chain_node_candidates(catalog(), [IND1], max_candidates=0)


class ModelOutputGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.subjects = [subject(COM1, 0), subject(COM2, 1)]
        self.candidates = {
            0: [CandidateChoice(key="I1", target_id=IND1, name="半导体", definition="定义")],
            1: [CandidateChoice(key="I1", target_id=IND1, name="半导体", definition="定义")],
        }

    def test_response_must_cover_every_input_exactly_once(self) -> None:
        response = ModelSelectionResponse(
            items=[ModelSelectionItem(input_index=0, selections=[], no_match_reason="无法确认")]
        )

        with self.assertRaisesRegex(ValueError, "exactly cover input indexes"):
            validate_model_response(self.subjects, self.candidates, response, max_selections=3)

    def test_duplicate_input_index_is_rejected(self) -> None:
        response = ModelSelectionResponse(
            items=[
                ModelSelectionItem(input_index=0, selections=[], no_match_reason="无法确认"),
                ModelSelectionItem(input_index=0, selections=[], no_match_reason="仍无法确认"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "exactly cover input indexes"):
            validate_model_response(self.subjects, self.candidates, response, max_selections=3)

    def test_free_form_or_cross_company_candidate_key_is_rejected(self) -> None:
        response = ModelSelectionResponse(
            items=[
                ModelSelectionItem(
                    input_index=0,
                    selections=[
                        {
                            "candidate_key": "I999",
                            "confidence": "HIGH",
                            "rationale": "模型编造了候选",
                            "supporting_company_fields": ["name"],
                        }
                    ],
                ),
                ModelSelectionItem(input_index=1, selections=[], no_match_reason="无法确认"),
            ]
        )

        with self.assertRaisesRegex(ValueError, "unknown candidate key"):
            validate_model_response(self.subjects, self.candidates, response, max_selections=3)

    def test_low_confidence_is_retained_for_audit_but_not_accepted(self) -> None:
        response = ModelSelectionResponse(
            items=[
                ModelSelectionItem(
                    input_index=0,
                    selections=[
                        {
                            "candidate_key": "I1",
                            "confidence": "LOW",
                            "rationale": "仅名称弱相关",
                            "supporting_company_fields": ["name"],
                        }
                    ],
                ),
                ModelSelectionItem(input_index=1, selections=[], no_match_reason="无法确认"),
            ]
        )

        validated = validate_model_response(self.subjects, self.candidates, response, max_selections=3)

        self.assertEqual(validated[0].status, DecisionStatus.LOW_CONFIDENCE)
        self.assertEqual(validated[0].accepted_targets, [])
        self.assertEqual(validated[1].status, DecisionStatus.NO_MATCH)


class DecisionJournalTest(unittest.TestCase):
    def _manifest(self) -> ProjectionRunManifest:
        return ProjectionRunManifest(
            snapshot_id="a" * 64,
            company_snapshot_fingerprint="b" * 64,
            target_catalog_fingerprint=catalog().fingerprint(),
            company_ids=[COM1],
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v1",
            model_id="deepseek-v4-flash",
            prompt_contract_version="company-target-selection/v1",
            max_chain_candidates=20,
            created_at=NOW,
        )

    def _decision(self) -> CompanyInferenceDecision:
        return finalize_company_decision(
            company=subject(),
            industry_result={
                "status": DecisionStatus.MAPPED,
                "accepted_targets": [
                    {
                        "target_id": IND1,
                        "confidence": Confidence.HIGH,
                        "rationale": "公司名称明确为半导体",
                        "supporting_company_fields": ["name"],
                    }
                ],
                "rejected_targets": [],
                "reason": None,
            },
            chain_node_result={
                "status": DecisionStatus.NO_MATCH,
                "accepted_targets": [],
                "rejected_targets": [],
                "reason": "没有足够具体的产业链环节",
            },
            root_industry_candidate_ids=[IND1],
            selected_root_industry_ids=[IND1],
            industry_candidate_ids=[IND1, IND2],
            chain_node_candidate_ids=[CND1, CND2],
            manifest=self._manifest(),
            decided_at=NOW,
        )

    def test_identical_retry_reuses_frozen_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())

            first_path, first_created = journal.freeze(self._decision())
            second_path, second_created = journal.freeze(self._decision())

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_path, second_path)
            self.assertEqual(journal.load(COM1), self._decision())

    def test_freeze_reuses_the_manifest_loaded_for_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())

            with patch.object(
                ProjectionRunManifest,
                "model_validate_json",
                side_effect=AssertionError("manifest was reparsed"),
            ):
                journal.freeze(self._decision())
                journal.freeze(self._decision())

    def test_freeze_reuses_the_manifest_fingerprint_computed_for_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            decision = self._decision()

            with patch.object(
                ProjectionRunManifest,
                "fingerprint",
                side_effect=AssertionError("manifest fingerprint was recomputed"),
            ):
                journal.freeze(decision)
                journal.freeze(decision)

    def test_conflicting_retry_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            journal.freeze(self._decision())
            conflicting = self._decision().model_copy(update={"decided_at": NOW.replace(minute=1)})

            with self.assertRaisesRegex(ValueError, "identity conflict"):
                journal.freeze(conflicting)

    def test_load_rejects_decision_provenance_outside_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            path, _ = journal.freeze(self._decision())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["snapshot_id"] = "f" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "provenance does not match"):
                journal.load(COM1)

    def test_load_rejects_decision_id_that_does_not_match_frozen_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            path, _ = journal.freeze(self._decision())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["decision_id"] = "f" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "decision_id does not match"):
                journal.load(COM1)

    def test_load_rejects_input_index_that_differs_from_manifest_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            path, _ = journal.freeze(self._decision())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["input_index"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "input_index differs"):
                journal.load(COM1)

    def test_load_rejects_terminal_status_that_disagrees_with_stage_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            path, _ = journal.freeze(self._decision())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = DecisionStatus.NO_MATCH
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "status disagrees"):
                journal.load(COM1)

    def test_manifest_fingerprint_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            journal.open_or_create(self._manifest())
            changed = self._manifest().model_copy(update={"target_catalog_fingerprint": "c" * 64})

            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                journal.open_or_create(changed)

    def test_decision_rejects_a_selected_target_outside_the_offered_candidates(self) -> None:
        payload = self._decision().model_dump(mode="json")
        payload["candidates"]["industry_candidate_ids"] = [IND2]

        with self.assertRaisesRegex(ValidationError, "Industry decision contains a target that was not offered"):
            CompanyInferenceDecision.model_validate(payload)

    def test_candidate_audit_must_reconstruct_from_the_frozen_catalog(self) -> None:
        payload = self._decision().model_dump(mode="json")
        payload["candidates"]["chain_node_candidate_ids"] = [CND2]
        decision = CompanyInferenceDecision.model_validate(payload)

        with self.assertRaisesRegex(ValueError, "ChainNode candidates differ"):
            validate_decision_candidate_scope(decision, catalog(), self._manifest())


class _FakeSelector:
    def __init__(self, responses: list[ModelSelectionResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[str] = []

    async def select_industry_roots(self, subjects, candidates):  # type: ignore[no-untyped-def]
        self.calls.append(f"root:{len(subjects)}:{len(candidates)}")
        return self.responses.popleft()

    async def select_industries(self, subjects, candidates_by_input):  # type: ignore[no-untyped-def]
        self.calls.append(f"industry:{len(subjects)}:{sum(map(len, candidates_by_input.values()))}")
        return self.responses.popleft()

    async def select_chain_nodes(self, subjects, candidates_by_input):  # type: ignore[no-untyped-def]
        self.calls.append(f"chain:{len(subjects)}:{sum(map(len, candidates_by_input.values()))}")
        return self.responses.popleft()


class InferenceRunnerTest(unittest.IsolatedAsyncioTestCase):
    def _manifest(self, subjects: list[CompanySubject]) -> ProjectionRunManifest:
        return ProjectionRunManifest(
            snapshot_id="a" * 64,
            company_snapshot_fingerprint=company_snapshot_fingerprint(subjects),
            target_catalog_fingerprint=catalog().fingerprint(),
            company_ids=[item.company_id for item in subjects],
            ontology_version="reasoning-ontology/v5",
            policy_version="company-projection-policy/v1",
            model_id="deepseek-v4-flash",
            prompt_contract_version="company-target-selection/v1",
            max_chain_candidates=20,
            created_at=NOW,
        )

    async def test_completed_decisions_are_reused_without_model_calls(self) -> None:
        subjects = [subject()]
        selector = _FakeSelector(
            [
                ModelSelectionResponse(
                    items=[
                        ModelSelectionItem(
                            input_index=0,
                            selections=[
                                {
                                    "candidate_key": "I1",
                                    "confidence": "HIGH",
                                    "rationale": "名称明确属于半导体",
                                    "supporting_company_fields": ["name"],
                                }
                            ],
                        )
                    ]
                ),
                ModelSelectionResponse(
                    items=[
                        ModelSelectionItem(
                            input_index=0,
                            selections=[
                                {
                                    "candidate_key": "I1",
                                    "confidence": "HIGH",
                                    "rationale": "名称明确属于半导体",
                                    "supporting_company_fields": ["name"],
                                }
                            ],
                        )
                    ]
                ),
                ModelSelectionResponse(
                    items=[
                        ModelSelectionItem(
                            input_index=0,
                            selections=[
                                {
                                    "candidate_key": "N1",
                                    "confidence": "MEDIUM",
                                    "rationale": "业务名称支持晶圆制造环节",
                                    "supporting_company_fields": ["name"],
                                }
                            ],
                        )
                    ]
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            first = await infer_companies(
                subjects,
                catalog(),
                self._manifest(subjects),
                journal,
                selector,
                decided_at=NOW,
                industry_batch_size=20,
                max_chain_candidates=20,
            )
            second = await infer_companies(
                subjects,
                catalog(),
                self._manifest(subjects),
                journal,
                selector,
                decided_at=NOW,
                industry_batch_size=20,
                max_chain_candidates=20,
            )

        self.assertEqual(first, second)
        self.assertEqual(selector.calls, ["root:1:1", "industry:1:2", "chain:1:2"])
        self.assertEqual(first[0].industry.accepted_targets[0].target_id, IND1)
        self.assertEqual(first[0].chain_node.accepted_targets[0].target_id, CND1)

    async def test_no_industry_match_does_not_call_chain_selector(self) -> None:
        subjects = [subject()]
        selector = _FakeSelector(
            [
                ModelSelectionResponse(
                    items=[ModelSelectionItem(input_index=0, selections=[], no_match_reason="名称不足以判断")]
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            decisions = await infer_companies(
                subjects,
                catalog(),
                self._manifest(subjects),
                DecisionJournal(Path(directory)),
                selector,
                decided_at=NOW,
                industry_batch_size=20,
                max_chain_candidates=20,
            )

        self.assertEqual(selector.calls, ["root:1:1"])
        self.assertEqual(decisions[0].status, DecisionStatus.NO_MATCH)
        self.assertEqual(decisions[0].chain_node.status, DecisionStatus.NO_CANDIDATE)

    async def test_manifest_must_match_exact_subject_snapshot(self) -> None:
        subjects = [subject()]
        manifest = self._manifest(subjects).model_copy(update={"company_snapshot_fingerprint": "f" * 64})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Company snapshot fingerprint"):
                await infer_companies(
                    subjects,
                    catalog(),
                    manifest,
                    DecisionJournal(Path(directory)),
                    _FakeSelector([]),
                    decided_at=NOW,
                    industry_batch_size=20,
                    max_chain_candidates=20,
                )

    async def test_candidate_bound_must_match_the_frozen_manifest(self) -> None:
        subjects = [subject()]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "max_chain_candidates differs"):
                await infer_companies(
                    subjects,
                    catalog(),
                    self._manifest(subjects),
                    DecisionJournal(Path(directory)),
                    _FakeSelector([]),
                    decided_at=NOW,
                    industry_batch_size=20,
                    max_chain_candidates=10,
                )

    async def test_bounded_infer_call_freezes_progress_without_allowing_an_incomplete_run(self) -> None:
        subjects = [subject(COM1, 0), subject(COM2, 1)]
        selector = _FakeSelector(
            [
                ModelSelectionResponse(
                    items=[ModelSelectionItem(input_index=0, selections=[], no_match_reason="无法确认")]
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = DecisionJournal(Path(directory))
            decisions = await infer_companies(
                subjects,
                catalog(),
                self._manifest(subjects),
                journal,
                selector,
                decided_at=NOW,
                industry_batch_size=20,
                max_chain_candidates=20,
                max_new_decisions=1,
            )

            self.assertEqual([item.company_id for item in decisions], [COM1])
            with self.assertRaises(FileNotFoundError):
                journal.assert_complete()


if __name__ == "__main__":
    unittest.main()
