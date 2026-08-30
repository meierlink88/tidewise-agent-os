"""Configured Graphiti LLM adapter for candidate-key-only Company decisions."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence

from graphiti_core import Graphiti
from graphiti_core.prompts.models import Message

from capabilities.company.internal.models import (
    CandidateChoice,
    CompanySubject,
    ModelSelectionResponse,
)
from sematica.projection.runtime import GRAPHITI_GROUP_ID

logger = logging.getLogger(__name__)
STRUCTURED_OUTPUT_ATTEMPTS = 3


class GraphitiCompanyTargetSelector:
    """Ask the configured model to choose only short keys from frozen candidates."""

    def __init__(self, graphiti: Graphiti) -> None:
        self._client = graphiti.clients.llm_client

    async def _structured(
        self,
        messages: list[Message],
        *,
        prompt_name: str,
        max_tokens: int,
    ) -> ModelSelectionResponse:
        last_error: Exception | None = None
        for attempt in range(1, STRUCTURED_OUTPUT_ATTEMPTS + 1):
            try:
                async with asyncio.timeout(120):
                    result = await self._client.generate_response(
                        messages,
                        response_model=ModelSelectionResponse,
                        max_tokens=max_tokens,
                        group_id=GRAPHITI_GROUP_ID,
                        prompt_name=prompt_name,
                    )
                return ModelSelectionResponse.model_validate(result)
            except Exception as exc:  # noqa: BLE001 - provider/schema boundary
                last_error = exc
                logger.warning(
                    "company_selection_retry prompt_name=%s attempt=%d max_attempts=%d error_type=%s",
                    prompt_name,
                    attempt,
                    STRUCTURED_OUTPUT_ATTEMPTS,
                    type(exc).__name__,
                )
                if attempt < STRUCTURED_OUTPUT_ATTEMPTS:
                    await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _company(subject: CompanySubject) -> dict[str, object]:
        return {
            "input_index": subject.input_index,
            "code": subject.code,
            "name": subject.name,
            "name_en": subject.name_en,
            "legal_name": subject.legal_name,
            "aliases": subject.aliases,
            "strategic_positioning": subject.strategic_positioning,
            "description": subject.description,
        }

    @staticmethod
    def _candidate(candidate: CandidateChoice) -> dict[str, object]:
        return {
            "key": candidate.key,
            "name": candidate.name,
            "definition": candidate.definition[:500],
            "context": candidate.context,
        }

    async def select_industry_roots(
        self,
        subjects: Sequence[CompanySubject],
        candidates: Sequence[CandidateChoice],
    ) -> ModelSelectionResponse:
        payload = {
            "companies": [self._company(subject) for subject in subjects],
            "root_industry_candidates": [self._candidate(candidate) for candidate in candidates],
        }
        return await self._structured(
            [
                Message(
                    role="system",
                    content=(
                        "先判断每家公司直接经营业务所属的一级行业范围，只能从给定 "
                        "root_industry_candidates 选择。"
                        "不得输出候选之外的 key，不得推导下游受益行业，不得为了覆盖率强行匹配。"
                        "可以依据公司名称、别名、已知公开业务身份和给定业务字段判断；不确定时 selections 为空。"
                        "每个 input_index 必须且只能返回一次，最多选择3个一级行业范围。"
                        "MEDIUM/HIGH 仅用于有可信直接经营依据的目标；弱名称联想必须标 LOW。"
                        "rationale 只写简短可审核理由，不输出隐藏思维过程。"
                    ),
                ),
                Message(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            ],
            prompt_name="tidewise_company_industry_root_selection_v1",
            max_tokens=min(6000, max(1600, len(subjects) * 500)),
        )

    async def select_industries(
        self,
        subjects: Sequence[CompanySubject],
        candidates_by_input: dict[int, Sequence[CandidateChoice]],
    ) -> ModelSelectionResponse:
        payload = {
            "items": [
                {
                    "company": self._company(subject),
                    "industry_candidates": [
                        self._candidate(candidate) for candidate in candidates_by_input[subject.input_index]
                    ],
                }
                for subject in subjects
            ]
        }
        return await self._structured(
            [
                Message(
                    role="system",
                    content=(
                        "在已选一级行业范围内判断每家公司的主要直接经营行业。每个 input_index "
                        "只能从该项给定的 industry_candidates 选择，不得跨公司复用 key，"
                        "不得输出候选之外的 key，不得推导下游受益行业，不得为了覆盖率强行匹配。"
                        "不确定时 selections 为空；每个 input_index 必须且只能返回一次，最多选择3个行业。"
                        "MEDIUM/HIGH 仅用于有可信直接经营依据的目标；弱名称联想必须标 LOW。"
                        "rationale 只写简短可审核理由，不输出隐藏思维过程。"
                    ),
                ),
                Message(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            ],
            prompt_name="tidewise_company_industry_selection_v2",
            max_tokens=min(8000, max(2400, len(subjects) * 700)),
        )

    async def select_chain_nodes(
        self,
        subjects: Sequence[CompanySubject],
        candidates_by_input: dict[int, Sequence[CandidateChoice]],
    ) -> ModelSelectionResponse:
        payload = {
            "items": [
                {
                    "company": self._company(subject),
                    "chain_node_candidates": [
                        self._candidate(candidate) for candidate in candidates_by_input[subject.input_index]
                    ],
                }
                for subject in subjects
            ]
        }
        return await self._structured(
            [
                Message(
                    role="system",
                    content=(
                        "判断每家公司直接参与的产业链环节。每个 input_index 只能从该项给定的 "
                        "chain_node_candidates 选择，不得跨公司复用 key，不得输出任何自由 ID，"
                        "不得把上下游影响或潜在受益当作直接参与。没有可信直接业务依据时 selections 为空。"
                        "每个 input_index 必须且只能返回一次，最多选择8个环节。"
                        "MEDIUM/HIGH 才表示可建立关系；弱联想必须标 LOW。"
                        "rationale 只写简短可审核理由，不输出隐藏思维过程。"
                    ),
                ),
                Message(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            ],
            prompt_name="tidewise_company_chain_node_selection_v1",
            max_tokens=min(8000, max(2400, len(subjects) * 900)),
        )


__all__ = ["GraphitiCompanyTargetSelector"]
