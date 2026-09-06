"""Lock the Reviewer request format and retain the existing local validation gate."""

import unittest
from unittest.mock import patch

from agno.agent._response import get_response_format
from agno.models.openai import OpenAIResponses
from agno.run import RunContext
from pydantic import ValidationError

from agents.title_curator import build_title_curator_agent
from capabilities.evidence import ArticleReviewDraft, EvidenceReviewDraft


class ReviewerStructuredOutputTest(unittest.TestCase):
    def test_agent_sends_strict_nested_schema_not_json_object(self) -> None:
        with patch("agents.title_curator.get_postgres_db"):
            agent = build_title_curator_agent()
        context = RunContext(run_id="schema-probe", session_id="schema-probe", output_schema=agent.output_schema)
        response_format = get_response_format(agent, run_context=context)
        self.assertIs(response_format, EvidenceReviewDraft)
        self.assertTrue(agent.structured_outputs)
        self.assertFalse(agent.use_json_mode)
        assert isinstance(agent.model, OpenAIResponses)
        params = agent.model.get_request_params(response_format=response_format)
        output_format = params["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertIs(output_format["strict"], True)
        schema = output_format["schema"]
        self.assertEqual(set(schema["required"]), {"is_relevant", "extraction"})
        self.assertNotIn("article_key", schema["properties"])
        extraction = schema["$defs"]["EvidenceExtractionDraft"]
        self.assertEqual(set(extraction["required"]), {"raw_evidence", "evidences"})

        def check_objects(node):
            if isinstance(node, dict):
                # The proxy/upstream rejects description siblings alongside $ref.
                if "$ref" in node:
                    self.assertEqual(set(node), {"$ref"})
                if node.get("type") == "object":
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    check_objects(value)
            elif isinstance(node, list):
                for value in node:
                    check_objects(value)

        check_objects(schema)
        self.assertTrue(schema["properties"]["extraction"]["description"])
        self.assertEqual(agent.retries, 0)

    def test_llm_cannot_return_a_machine_identity(self) -> None:
        with self.assertRaises(ValidationError):
            EvidenceReviewDraft.model_validate({"article_key": "invented", "is_relevant": False, "extraction": None})

    def test_reported_corrupt_envelope_is_not_repaired_or_accepted(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            ArticleReviewDraft.model_validate(
                {
                    "article_key": "test-article",
                    "is_relevant": True,
                    "extraction": {"raw_evidenceхийн uncertainties": {"is_original": True}},
                }
            )
        errors = {(tuple(error["loc"]), error["type"]) for error in caught.exception.errors()}
        self.assertEqual(
            errors,
            {
                (("extraction", "raw_evidence"), "missing"),
                (("extraction", "evidences"), "missing"),
                (("extraction", "raw_evidenceхийн uncertainties"), "extra_forbidden"),
            },
        )

    def test_irrelevant_null_and_relevant_empty_evidence_remain_valid(self) -> None:
        ArticleReviewDraft(article_key="irrelevant", is_relevant=False, extraction=None)
        draft = ArticleReviewDraft.model_validate(
            {
                "article_key": "empty",
                "is_relevant": True,
                "extraction": {
                    "raw_evidence": {"category_code": "POLICY", "is_original": True, "quoted_source_name": None},
                    "evidences": [],
                },
            }
        )
        assert draft.extraction is not None
        self.assertEqual(draft.extraction.evidences, [])
        with self.assertRaises(ValidationError):
            ArticleReviewDraft(article_key="missing", is_relevant=True, extraction=None)
