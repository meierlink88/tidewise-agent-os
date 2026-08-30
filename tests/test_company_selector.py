"""Tests for the bounded Company model selection adapter."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import cast

from graphiti_core.prompts.models import Message

from capabilities.company import CandidateChoice, CompanySubject, GraphitiCompanyTargetSelector

COM_ID = "COM00000000-0000-4000-8000-000000000001"
IND_ID = "IND00000000-0000-4000-8000-000000000001"
CND_ID = "CND00000000-0000-4000-8000-000000000001"


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate_response(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, **kwargs})
        return {
            "items": [
                {
                    "input_index": 0,
                    "selections": [
                        {
                            "candidate_key": "I1" if "industry" in str(kwargs["prompt_name"]) else "N1",
                            "confidence": "HIGH",
                            "rationale": "名称与候选业务直接一致",
                            "supporting_company_fields": ["name"],
                        }
                    ],
                    "no_match_reason": None,
                }
            ]
        }


class _Clients:
    def __init__(self, llm: _FakeLLM) -> None:
        self.llm_client = llm


class _Graphiti:
    def __init__(self, llm: _FakeLLM) -> None:
        self.clients = _Clients(llm)


def _subject() -> CompanySubject:
    return CompanySubject(
        input_index=0,
        company_id=COM_ID,
        code="TEST",
        name="测试半导体股份有限公司",
        name_en=None,
        legal_name=None,
        aliases=[],
        registration_country_id=None,
        strategic_positioning=None,
        description=None,
        source_updated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )


class CompanySelectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_industry_root_prompt_exposes_short_keys_but_not_graph_ids(self) -> None:
        llm = _FakeLLM()
        selector = GraphitiCompanyTargetSelector(_Graphiti(llm))  # type: ignore[arg-type]

        result = await selector.select_industry_roots(
            [_subject()],
            [CandidateChoice(key="I1", target_id=IND_ID, name="半导体", definition="芯片研发制造")],
        )

        messages = cast(list[Message], llm.calls[0]["messages"])
        rendered = "\n".join(message.content for message in messages)
        self.assertEqual(result.items[0].selections[0].candidate_key, "I1")
        self.assertIn('"key":"I1"', rendered)
        self.assertNotIn(IND_ID, rendered)
        self.assertEqual(llm.calls[0]["prompt_name"], "tidewise_company_industry_root_selection_v1")

    async def test_detailed_industry_prompt_is_partitioned_by_company_input(self) -> None:
        llm = _FakeLLM()
        selector = GraphitiCompanyTargetSelector(_Graphiti(llm))  # type: ignore[arg-type]

        result = await selector.select_industries(
            [_subject()],
            {0: [CandidateChoice(key="I1", target_id=IND_ID, name="半导体", definition="芯片研发制造")]},
        )

        messages = cast(list[Message], llm.calls[0]["messages"])
        rendered = "\n".join(message.content for message in messages)
        self.assertEqual(result.items[0].selections[0].candidate_key, "I1")
        self.assertIn('"industry_candidates"', rendered)
        self.assertNotIn(IND_ID, rendered)
        self.assertEqual(llm.calls[0]["prompt_name"], "tidewise_company_industry_selection_v2")

    async def test_chain_prompt_is_partitioned_by_company_input(self) -> None:
        llm = _FakeLLM()
        selector = GraphitiCompanyTargetSelector(_Graphiti(llm))  # type: ignore[arg-type]

        result = await selector.select_chain_nodes(
            [_subject()],
            {
                0: [
                    CandidateChoice(
                        key="N1",
                        target_id=CND_ID,
                        name="晶圆制造",
                        definition="晶圆制造环节",
                        context=["行业：半导体", "产业链：集成电路产业链"],
                    )
                ]
            },
        )

        messages = cast(list[Message], llm.calls[0]["messages"])
        rendered = "\n".join(message.content for message in messages)
        self.assertEqual(result.items[0].selections[0].candidate_key, "N1")
        self.assertIn("晶圆制造", rendered)
        self.assertIn("集成电路产业链", rendered)
        self.assertNotIn(CND_ID, rendered)
        self.assertEqual(llm.calls[0]["prompt_name"], "tidewise_company_chain_node_selection_v1")


if __name__ == "__main__":
    unittest.main()
