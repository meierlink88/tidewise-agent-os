"""Configured Graphiti LLM adapters for controlled Event Analysis."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal, TypeVar, cast

from graphiti_core import Graphiti
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from sematica.analysis.event.contracts import (
    AnchorSignalSelection,
    CandidateSet,
    DirectSignalDraft,
    EventAnalysisInput,
    EventClassification,
    SignalCritique,
    SignalDetailDraft,
    SignalProposal,
)
from sematica.projection.runtime import GRAPHITI_GROUP_ID

StructuredModel = TypeVar("StructuredModel", bound=BaseModel)
logger = logging.getLogger(__name__)
STRUCTURED_OUTPUT_ATTEMPTS = 3


class GraphitiEventAnalysisLLM:
    """Reuse the configured Graphiti LLM for bounded structured decisions."""

    def __init__(self, graphiti: Graphiti) -> None:
        self._client = graphiti.clients.llm_client

    @staticmethod
    def _event_text(event: EventAnalysisInput) -> str:
        item = event.event.event
        return "\n".join(
            (
                f"标题：{item.title}",
                f"摘要：{item.summary}",
                f"主体：{' / '.join(item.semantic.actors)}",
                f"动作：{item.semantic.action}",
                f"对象：{' / '.join(item.semantic.objects)}",
                f"阶段：{item.semantic.stage}；模态：{item.modality}",
            )
        )

    async def _structured(
        self,
        messages: list[Message],
        response_model: type[StructuredModel],
        *,
        max_tokens: int,
        prompt_name: str,
    ) -> StructuredModel:
        last_error: Exception | None = None
        for attempt in range(1, STRUCTURED_OUTPUT_ATTEMPTS + 1):
            try:
                async with asyncio.timeout(120):
                    result = await self._client.generate_response(
                        messages,
                        response_model=response_model,
                        max_tokens=max_tokens,
                        group_id=GRAPHITI_GROUP_ID,
                        prompt_name=prompt_name,
                    )
                return response_model.model_validate(result)
            except Exception as exc:  # noqa: BLE001 - provider/schema boundary
                last_error = exc
                logger.warning(
                    "event_analysis_structured_output_retry prompt_name=%s attempt=%d max_attempts=%d error_type=%s",
                    prompt_name,
                    attempt,
                    STRUCTURED_OUTPUT_ATTEMPTS,
                    type(exc).__name__,
                )
                if attempt < STRUCTURED_OUTPUT_ATTEMPTS:
                    await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    async def classify(self, event: EventAnalysisInput) -> EventClassification:
        messages = [
            Message(
                role="system",
                content=(
                    "Classify one atomic investment-research Event by the layer where its "
                    "real-world actor/action/object occurs. Choose exactly one event_class: "
                    "GEOPOLITICAL, MACRO_ECONOMIC, INDUSTRY_CHAIN, CHAIN_NODE, or COMPANY. "
                    "Do not classify by inferred downstream effects. Also propose bounded anchor "
                    "type hints, VariableGroup hints, and short Chinese retrieval queries. Return "
                    "one JSON object with exactly: event_class; confidence (LOW|MEDIUM|HIGH); "
                    "anchor_type_hints (Country|Region|GeopoliticRivalry|MacroEconomic|"
                    "IndustryChain|ChainNode|Concept); variable_group_hints (DEMAND|"
                    "SUPPLY_CAPACITY|PRICE_PROFITABILITY|CAPITAL_CYCLE|TECHNOLOGY|"
                    "COMPETITION_SECURITY|MACRO_POLICY|GEOPOLITICAL); retrieval_queries; rationale."
                ),
            ),
            Message(
                role="user",
                content=self._event_text(event),
            ),
        ]
        result = await self._structured(
            messages,
            EventClassification,
            max_tokens=1200,
            prompt_name="tidewise_event_classification_v1",
        )
        return result

    async def extract(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        candidates: CandidateSet,
    ) -> list[SignalProposal]:
        variables = {f"V{index}": item for index, item in enumerate(candidates.variables, 1)}
        drafts: list[DirectSignalDraft] = []
        variable_options = [
            {
                "key": key,
                "name": item.name,
                "variable_group": item.variable_group.value,
            }
            for key, item in variables.items()
        ]
        for anchor in candidates.anchors:
            selection = await self._structured(
                [
                    Message(
                        role="system",
                        content=(
                            "判断该事件是否直接支持这一投研锚点的某个候选变量信号。"
                            "不做产业链传导，不推导投资价值、公司或证券。若成立只选一个变量key。"
                            "锚点必须是事件明示对象或它的标准同义映射；仅因语义相近不成立。"
                            '只返回JSON：{"has_signal":true,"variable_key":"V1",'
                            '"rationale":"..."}；不成立时variable_key为null。'
                        ),
                    ),
                    Message(
                        role="user",
                        content=json.dumps(
                            {
                                "event": self._event_text(event),
                                "anchor": {
                                    "name": anchor.name,
                                    "entity_type": anchor.entity_type.value,
                                },
                                "variables": variable_options,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                ],
                AnchorSignalSelection,
                max_tokens=1200,
                prompt_name="tidewise_anchor_signal_selection_v1",
            )
            if not selection.has_signal:
                continue
            assert selection.variable_key is not None
            variable = variables.get(selection.variable_key)
            if variable is None:
                raise ValueError("Signal selection references an unknown candidate UUID")
            detail_payload = {
                "event": self._event_text(event),
                "anchor": {
                    "uuid": anchor.uuid,
                    "name": anchor.name,
                    "entity_type": anchor.entity_type.value,
                },
                "variable": {
                    "uuid": variable.uuid,
                    "variable_id": variable.variable_id,
                    "name": variable.name,
                    "definition": variable.definition,
                },
            }
            detail = await self._structured(
                [
                    Message(
                        role="system",
                        content=(
                            "Describe one direct Signal for the already selected Variable/Anchor pair. "
                            "Use only the Event. Do not propagate along topology or infer investment, "
                            "company, security, price, or valuation conclusions. expected_duration_days "
                            "is an estimated impact window, not Graphiti invalid_at. Return one JSON "
                            "object with exactly: fact; direction (UP|DOWN|MIXED|STABLE|UNKNOWN); "
                            "Direction always describes the selected Variable itself as defined: "
                            "UP means that Variable increases or strengthens, DOWN means it decreases "
                            "or eases, MIXED means the Event creates material opposing direct effects, "
                            "and STABLE means no material change. It never means bullish/bearish and "
                            "never describes the Event action intensity. The fact and mechanism must "
                            "state the same direction unambiguously. "
                            "magnitude (LOW|MEDIUM|HIGH|UNKNOWN); impact_onset_days (0..1095, "
                            "relative to the Event effective/occurrence time); impact_peak_days "
                            "(required and not before onset); expected_duration_days (1..1095); "
                            "mechanism; duration_basis; assumptions (array); invalidation_conditions "
                            "(non-empty array); provenance_confidence; mechanism_confidence; and "
                            "temporal_confidence (each LOW|MEDIUM|HIGH)."
                        ),
                    ),
                    Message(
                        role="user",
                        content=json.dumps(detail_payload, ensure_ascii=False, sort_keys=True),
                    ),
                ],
                SignalDetailDraft,
                max_tokens=3000,
                prompt_name="tidewise_direct_signal_detail_v1",
            )
            critique = await self._structured(
                [
                    Message(
                        role="system",
                        content=(
                            "Independently challenge one proposed direct Signal. Accept only when "
                            "the Event itself explicitly supports the supplied Anchor/Variable pair "
                            "or uses an unambiguous standard synonym. Reject topology propagation, "
                            "cross-variable inference, investment conclusions, and unsupported "
                            "timing or mechanism. Also reject when direction conflicts with the "
                            "Variable definition, fact, or mechanism. UP means the selected Variable "
                            "itself increases/strengthens; DOWN means it decreases/eases; MIXED requires "
                            "opposing material direct effects; STABLE requires no material change. "
                            "Return one JSON object with exactly: accepted "
                            "(boolean), reason_codes (short uppercase strings)."
                        ),
                    ),
                    Message(
                        role="user",
                        content=json.dumps(
                            {**detail_payload, "proposal": detail.model_dump(mode="json")},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                ],
                SignalCritique,
                max_tokens=1200,
                prompt_name="tidewise_direct_signal_critic_v1",
            )
            if not critique.accepted:
                continue
            drafts.append(
                DirectSignalDraft(
                    anchor_uuid=anchor.uuid,
                    variable_uuid=variable.uuid,
                    **detail.model_dump(),
                )
            )
            if len(drafts) == 3:
                break
        event_time = (
            event.event.event.semantic.effective_at or event.event.event.occurred_at or event.event.event.announced_at
        )
        assert event_time is not None
        modality = cast(
            Literal["ACTUAL", "ANTICIPATED", "SOURCE_FORECAST", "ASSUMED"],
            {
                "FACT": "ACTUAL",
                "PLAN": "ANTICIPATED",
                "SPEC": "ASSUMED",
            }[event.event.event.modality],
        )
        return [draft.proposal(event_time=event_time, assertion_modality=modality) for draft in drafts]
