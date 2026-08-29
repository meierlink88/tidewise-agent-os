"""Adapt an Agno Registry model to Graphiti's public LLM client contract."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from agno.models.message import Message as AgnoMessage
from agno.models.response import ModelResponse
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from pydantic import BaseModel, ValidationError


class AgnoGraphitiLLM(LLMClient):
    """Let Graphiti native pipelines use the exact model registered by AgentOS."""

    def __init__(self, model: Any, *, max_tokens: int = 8192) -> None:
        model_id = str(getattr(model, "id", "agno-model"))
        super().__init__(
            config=LLMConfig(
                model=model_id,
                small_model=model_id,
                temperature=0,
                max_tokens=max_tokens,
            )
        )
        self._model = model
        self._max_tokens = max_tokens

    async def _respond(self, messages: list[AgnoMessage], *, max_tokens: int) -> ModelResponse:
        """Use an isolated model view so one Graphiti call cannot mutate Registry state."""

        call_model = copy.copy(self._model)
        call_model.max_tokens = min(max_tokens, self._max_tokens)
        return await call_model.aresponse(messages=messages, response_format=None)

    async def _generate_response(
        self,
        messages: list[Any],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 16384,
        model_size: Any = None,
    ) -> dict[str, Any]:
        del model_size
        agno_messages = [AgnoMessage(role=message.role, content=message.content) for message in messages]
        if response_model is not None:
            schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, sort_keys=True)
            agno_messages.append(
                AgnoMessage(
                    role="system",
                    content=(
                        "Return exactly one JSON object satisfying this schema. Do not return markdown, "
                        f"the schema itself, or explanatory text. SCHEMA: {schema}"
                    ),
                )
            )
        response = await self._respond(agno_messages, max_tokens=max_tokens)
        try:
            return self._validated_payload(response, response_model)
        except ValueError:
            if response_model is None:
                raise
            agno_messages.extend(
                [
                    AgnoMessage(role="assistant", content=self._response_text(response)),
                    AgnoMessage(
                        role="user",
                        content=(
                            "The previous response was not a data instance of the requested schema. "
                            "Return the actual extracted values only. Do not return JSON Schema keys such as "
                            "properties, type, title, description, $defs, or additionalProperties."
                        ),
                    ),
                ]
            )
            corrected = await self._respond(agno_messages, max_tokens=max_tokens)
            try:
                return self._validated_payload(corrected, response_model)
            except ValueError as exc:
                raise ValueError("Agno model returned invalid Graphiti structured output after correction") from exc

    @staticmethod
    def _response_text(response: ModelResponse) -> str:
        value = response.parsed if response.parsed is not None else response.content
        if isinstance(value, BaseModel):
            return value.model_dump_json()
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            return value
        return ""

    @classmethod
    def _validated_payload(
        cls,
        response: ModelResponse,
        response_model: type[BaseModel] | None,
    ) -> dict[str, Any]:
        value = response.parsed if response.parsed is not None else response.content
        if isinstance(value, BaseModel):
            payload = value.model_dump(mode="json")
        elif isinstance(value, dict):
            payload = value
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("Agno model returned invalid Graphiti JSON") from exc
            if not isinstance(decoded, dict):
                raise ValueError("Agno model returned a non-object Graphiti response")
            payload = decoded
        else:
            raise ValueError("Agno model returned no Graphiti response")

        if response_model is None:
            return payload
        try:
            validated = response_model.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("Agno model returned invalid Graphiti structured output") from exc
        return validated.model_dump(mode="json")


class _Relevance(BaseModel):
    relevant: bool


class AgnoGraphitiReranker(CrossEncoderClient):
    """Use the same AgentOS model for Graphiti's native semantic reranking."""

    def __init__(self, model: Any, *, max_concurrency: int = 2, max_tokens: int = 256) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._model = model
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_tokens = max_tokens

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        async def score(passage: str) -> tuple[str, float]:
            call_model = copy.copy(self._model)
            call_model.max_tokens = self._max_tokens
            async with self._semaphore:
                response: ModelResponse = await call_model.aresponse(
                    messages=[
                        AgnoMessage(
                            role="system",
                            content="Judge whether the passage is relevant to the query. Return structured output.",
                        ),
                        AgnoMessage(
                            role="user",
                            content=(
                                f"QUERY:\n{query}\n\nPASSAGE:\n{passage}\n\n"
                                'Return only JSON: {"relevant": true} or {"relevant": false}.'
                            ),
                        ),
                    ],
                    response_format=None,
                )
            value = response.parsed if response.parsed is not None else response.content
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, _Relevance):
                value = _Relevance.model_validate(value)
            return passage, 1.0 if value.relevant else 0.0

        ranked = list(await asyncio.gather(*(score(passage) for passage in passages)))
        return sorted(ranked, key=lambda item: item[1], reverse=True)
