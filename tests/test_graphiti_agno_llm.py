"""Contract tests for the Agno-backed Graphiti LLM adapter."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from agno.models.response import ModelResponse
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from sematica.graphiti.agno_llm import AgnoGraphitiLLM, AgnoGraphitiReranker


class StructuredAnswer(BaseModel):
    answer: str


class StructuredTags(BaseModel):
    tags: list[str]


class AgnoGraphitiLLMTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_registered_agno_model_for_graphiti_structured_output(self) -> None:
        model = AsyncMock()
        model.id = "registered-deepseek"
        model.aresponse.return_value = ModelResponse(parsed=StructuredAnswer(answer="ok"))
        client = AgnoGraphitiLLM(model)

        result = await client._generate_response(
            [Message(role="system", content="system"), Message(role="user", content="question")],
            response_model=StructuredAnswer,
            max_tokens=512,
        )

        self.assertEqual(result, {"answer": "ok"})
        call = model.aresponse.await_args
        self.assertEqual([item.role for item in call.kwargs["messages"]], ["system", "user", "system"])
        self.assertEqual(call.kwargs["response_format"], {"type": "json_object"})
        self.assertIn("Return exactly one JSON object", call.kwargs["messages"][-1].content)

    async def test_rejects_unstructured_or_schema_invalid_model_output(self) -> None:
        model = AsyncMock()
        model.id = "registered-deepseek"
        model.aresponse.return_value = ModelResponse(content="not-json", parsed=None)
        client = AgnoGraphitiLLM(model)

        with self.assertRaises(ValueError):
            await client._generate_response(
                [Message(role="user", content="question")],
                response_model=StructuredAnswer,
            )

        self.assertEqual(model.aresponse.await_count, 2)

    async def test_corrects_a_model_that_returns_the_schema_instead_of_data(self) -> None:
        model = AsyncMock()
        model.id = "registered-deepseek"
        model.aresponse.side_effect = [
            ModelResponse(content='{"type":"object","properties":{"answer":{"type":"string"}}}'),
            ModelResponse(content='{"answer":"corrected"}'),
        ]
        client = AgnoGraphitiLLM(model)

        result = await client._generate_response(
            [Message(role="user", content="question")],
            response_model=StructuredAnswer,
        )

        self.assertEqual(result, {"answer": "corrected"})
        correction = model.aresponse.await_args_list[1].kwargs["messages"][-1]
        self.assertIn("not a data instance", correction.content)

    async def test_correction_names_only_safe_schema_validation_issues(self) -> None:
        model = AsyncMock()
        model.id = "registered-deepseek"
        model.aresponse.side_effect = [
            ModelResponse(content='{"tags":"macro"}'),
            ModelResponse(content='{"tags":["macro"]}'),
        ]
        client = AgnoGraphitiLLM(model)

        result = await client._generate_response(
            [Message(role="user", content="question")],
            response_model=StructuredTags,
        )

        self.assertEqual(result, {"tags": ["macro"]})
        correction = model.aresponse.await_args_list[1].kwargs["messages"][-1]
        self.assertIn("tags: list_type", correction.content)

    async def test_applies_the_lower_configured_or_per_call_token_cap_without_mutating_registry_model(self) -> None:
        class Model:
            id = "registered-deepseek"
            max_tokens = None

            def __init__(self) -> None:
                self.observed: list[int | None] = []

            async def aresponse(self, **kwargs):
                del kwargs
                self.observed.append(self.max_tokens)
                return ModelResponse(content='{"answer":"ok"}')

        model = Model()
        client = AgnoGraphitiLLM(model, max_tokens=1024)

        await client._generate_response([Message(role="user", content="question")], max_tokens=4096)
        await client._generate_response([Message(role="user", content="question")], max_tokens=256)

        self.assertEqual(model.observed, [1024, 256])
        self.assertIsNone(model.max_tokens)

    async def test_reranker_limits_parallel_model_calls(self) -> None:
        class Model:
            id = "registered-deepseek"
            max_tokens = None
            active = 0
            peak = 0

            async def aresponse(self, **kwargs):
                del kwargs
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)
                await asyncio.sleep(0.01)
                type(self).active -= 1
                return ModelResponse(content='{"relevant":true}')

        model = Model()
        ranked = await AgnoGraphitiReranker(model, max_concurrency=2).rank(
            "query",
            [f"passage-{index}" for index in range(6)],
        )

        self.assertEqual(len(ranked), 6)
        self.assertEqual(Model.peak, 2)
        self.assertIsNone(model.max_tokens)


if __name__ == "__main__":
    unittest.main()
