"""Variable catalog and Signal ownership invariants."""

import unittest

from pydantic import ValidationError

from sematica.analysis.event.contracts import SignalFactAttributes
from sematica.initialization.variable.projection import build_plan, load_catalog


class VariableSignalOwnershipTest(unittest.TestCase):
    def test_catalog_projects_no_industry_chain_applicability(self) -> None:
        catalog = load_catalog()
        plan = build_plan(catalog)

        self.assertEqual(catalog.catalog_version, "variable-catalog/v2")
        self.assertTrue(any("ChainNode" in item.allowed_anchor_types for item in catalog.items))
        self.assertTrue(all("IndustryChain" not in item.allowed_anchor_types for item in catalog.items))
        self.assertTrue(all("IndustryChain" not in node.attributes["allowed_anchor_types"] for node in plan.nodes))

    def test_signal_fact_contract_rejects_industry_chain_anchor(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cannot own a direct Signal Fact"):
            SignalFactAttributes.model_validate(
                {
                    "source_event_ids": ["EVT00000000-0000-4000-8000-000000000001"],
                    "event_class": "INDUSTRY_CHAIN",
                    "variable_id": "market_demand",
                    "anchor_type": "IndustryChain",
                    "anchor_business_id": "ICH00000000-0000-4000-8000-000000000001",
                    "direction": "UP",
                    "magnitude": "MEDIUM",
                    "derivation_type": "DERIVED",
                    "assertion_modality": "ACTUAL",
                    "impact_onset_earliest": "2026-09-01T00:00:00Z",
                    "impact_onset_latest": "2026-09-01T00:00:00Z",
                    "impact_peak_earliest": "2026-09-08T00:00:00Z",
                    "impact_peak_latest": "2026-09-08T00:00:00Z",
                    "expected_end_earliest": "2026-10-01T00:00:00Z",
                    "expected_end_latest": "2026-10-01T00:00:00Z",
                    "horizon_tags": ["SHORT"],
                    "mechanism": "需求变化。",
                    "duration_basis": "事件期间。",
                    "assumptions": [],
                    "invalidation_conditions": ["需求没有变化"],
                    "provenance_confidence": "HIGH",
                    "mechanism_confidence": "MEDIUM",
                    "temporal_confidence": "MEDIUM",
                    "methodology_version": "event-analysis/v1",
                }
            )


if __name__ == "__main__":
    unittest.main()
