"""Deterministic Report publication contract, projection, and publisher seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from capabilities.investment.internal.report_contract import (
    InvestmentReportArtifact,
    ReportAnchor,
    ReportConfidence,
    ReportEvidenceReference,
    ReportIndustryChain,
    ReportIndustryChainNode,
    ReportLayer,
    ReportNature,
    ReportReasoningStep,
    ReportResult,
    ReportTransmissionPath,
)


class PublicationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_pair(code: str, label: str, catalog: dict[str, str]) -> None:
    if catalog.get(code) != label:
        raise ValueError(f"invalid publication code/label pair: {code}/{label}")


class PublicationReportType(PublicationModel):
    code: Literal["investment_reasoning"] = "investment_reasoning"
    label: Literal["投研推理报告"] = "投研推理报告"


class PublicationResult(PublicationModel):
    code: Literal["warming", "cooling", "diverging", "pending"]
    label: Literal["升温", "降温", "分化", "待验证"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {"warming": "升温", "cooling": "降温", "diverging": "分化", "pending": "待验证"},
        )
        return self


class PublicationConfidence(PublicationModel):
    code: Literal["low", "medium", "high"]
    label: Literal["低", "中", "高"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(self.code, self.label, {"low": "低", "medium": "中", "high": "高"})
        return self


class PublicationTimeWindow(PublicationModel):
    code: Literal[
        "short",
        "medium",
        "long",
        "short_medium",
        "short_long",
        "medium_long",
        "short_medium_long",
        "follow_up",
    ]
    label: Literal["短期", "中期", "长期", "短期–中期", "短期–长期", "中期–长期", "短期–中期–长期", "后续周期"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {
                "short": "短期",
                "medium": "中期",
                "long": "长期",
                "short_medium": "短期–中期",
                "short_long": "短期–长期",
                "medium_long": "中期–长期",
                "short_medium_long": "短期–中期–长期",
                "follow_up": "后续周期",
            },
        )
        return self


class PublicationConclusionBasis(PublicationModel):
    code: Literal["direct_evidence", "reasoning_hypothesis", "no_directional_conclusion"]
    label: Literal["直接证据", "推理假设", "无方向性结论"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {
                "direct_evidence": "直接证据",
                "reasoning_hypothesis": "推理假设",
                "no_directional_conclusion": "无方向性结论",
            },
        )
        return self


class PublicationValidationStatus(PublicationModel):
    code: Literal["confirmed", "pending_validation"]
    label: Literal["已确认", "待验证"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(self.code, self.label, {"confirmed": "已确认", "pending_validation": "待验证"})
        return self


class PublicationEvidenceRole(PublicationModel):
    code: Literal["direct_support", "reasoning_support", "summary_support"]
    label: Literal["直接依据", "推导依据", "核心依据"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {"direct_support": "直接依据", "reasoning_support": "推导依据", "summary_support": "核心依据"},
        )
        return self


class PublicationTransmissionKind(PublicationModel):
    code: Literal["cross_layer_reasoning", "same_source_signal"]
    label: Literal["跨层推理", "同源信号"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {"cross_layer_reasoning": "跨层推理", "same_source_signal": "同源信号"},
        )
        return self


class PublicationTransmissionStatus(PublicationModel):
    code: Literal["established"] = "established"
    label: Literal["已形成传导"] = "已形成传导"


class PublicationTargetType(PublicationModel):
    code: Literal["macro_anchor", "industry_chain", "industry_chain_node"]
    label: Literal["宏观经济锚点", "产业链", "产业链节点"]

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        _check_pair(
            self.code,
            self.label,
            {
                "macro_anchor": "宏观经济锚点",
                "industry_chain": "产业链",
                "industry_chain_node": "产业链节点",
            },
        )
        return self


class PublicationEvidenceReference(PublicationModel):
    evidence_id: str = Field(pattern=r"^EVD[0-9a-f-]{36}$")
    role: PublicationEvidenceRole


class PublicationAnchor(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    current_state: str
    result: PublicationResult
    conclusion_basis: PublicationConclusionBasis
    validation_status: PublicationValidationStatus
    reasoning: str
    time_window: PublicationTimeWindow
    confidence: PublicationConfidence
    evidence_refs: list[PublicationEvidenceReference]


class PublicationReasoningStep(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    input: str
    mechanism: str
    output: str
    confidence: PublicationConfidence
    evidence_refs: list[PublicationEvidenceReference]


class PublicationTransmissionTarget(PublicationModel):
    target_type: PublicationTargetType
    target_local_key: str = Field(min_length=1, max_length=128)
    target_name: str = Field(min_length=1, max_length=500)
    result: PublicationResult


class PublicationTransmissionPath(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    source_conclusion: str
    targets: list[PublicationTransmissionTarget] = Field(min_length=1)
    transmission_logic: str
    transmission_kind: PublicationTransmissionKind
    confidence: PublicationConfidence
    status: PublicationTransmissionStatus = Field(default_factory=PublicationTransmissionStatus)


class PublicationTransmissionGroup(PublicationModel):
    summary: str
    paths: list[PublicationTransmissionPath]


class PublicationGeopoliticalDownwardTransmission(PublicationModel):
    to_macroeconomics: PublicationTransmissionGroup
    to_industry_chains: PublicationTransmissionGroup


class PublicationMacroeconomicDownwardTransmission(PublicationModel):
    to_industry_chains: PublicationTransmissionGroup


class PublicationLayerUncertainty(PublicationModel):
    counterevidence: str | None
    evidence_gap: str | None
    boundary: str | None
    reversal_condition: str | None


class PublicationLayerBase(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    conclusion: str
    result: PublicationResult
    time_window: PublicationTimeWindow
    confidence: PublicationConfidence
    affected_anchors: list[PublicationAnchor]
    reasoning_steps: list[PublicationReasoningStep]
    uncertainty: PublicationLayerUncertainty
    evidence_refs: list[PublicationEvidenceReference]


class PublicationGeopoliticalLayer(PublicationLayerBase):
    downward_transmission: PublicationGeopoliticalDownwardTransmission


class PublicationMacroeconomicLayer(PublicationLayerBase):
    downward_transmission: PublicationMacroeconomicDownwardTransmission


class PublicationIndustryChainNode(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    impact: str
    result: PublicationResult
    conclusion_basis: PublicationConclusionBasis
    validation_status: PublicationValidationStatus
    reasoning: str
    time_window: PublicationTimeWindow
    confidence: PublicationConfidence
    evidence_refs: list[PublicationEvidenceReference]


class PublicationIndustryChainEdge(PublicationModel):
    from_node_local_key: str = Field(min_length=1, max_length=128)
    to_node_local_key: str = Field(min_length=1, max_length=128)
    relation_label: str = Field(min_length=1, max_length=500)


class PublicationChainUncertainty(PublicationModel):
    counterevidence_and_gap: str | None
    stop_condition: str | None


class PublicationIndustryChain(PublicationModel):
    local_key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    conclusion: str
    result: PublicationResult
    time_window: PublicationTimeWindow
    confidence: PublicationConfidence
    path_summary: str | None
    accepted_hypothesis_summary: str | None
    nodes: list[PublicationIndustryChainNode]
    edges: list[PublicationIndustryChainEdge]
    uncertainty: PublicationChainUncertainty
    evidence_refs: list[PublicationEvidenceReference]

    @model_validator(mode="after")
    def validate_edge_closure(self) -> Self:
        node_keys = {node.local_key for node in self.nodes}
        for edge in self.edges:
            if edge.from_node_local_key not in node_keys or edge.to_node_local_key not in node_keys:
                raise ValueError("industry-chain edge does not close over published node local keys")
        return self


class PublicationReport(PublicationModel):
    report_type: PublicationReportType = Field(default_factory=PublicationReportType)
    generated_at: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    geopolitics: PublicationGeopoliticalLayer
    macroeconomics: PublicationMacroeconomicLayer
    industry_chains: list[PublicationIndustryChain]


class ReportPublicationRequest(PublicationModel):
    publisher_report_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    report: PublicationReport


class ReportPublicationReceipt(PublicationModel):
    report_id: str = Field(pattern=r"^RPT[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    published_at: datetime
    replayed: StrictBool

    @field_validator("published_at")
    @classmethod
    def published_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("published_at must be a UTC timestamp")
        return value


class ReportPublicationConflict(ValueError):
    """The same publisher identity was reused with divergent content."""


class ReportPublicationRejected(RuntimeError):
    """Data rejected a Report publication with a permanent client error."""


class ReportPublicationUnavailable(RuntimeError):
    """Data could not return a valid Report publication receipt."""


class ReportPublisher(Protocol):
    async def publish(self, request: ReportPublicationRequest) -> ReportPublicationReceipt: ...


class ReportPublicationEnvelope(PublicationModel):
    request_id: str = Field(min_length=1, max_length=128)
    result: ReportPublicationReceipt

    @field_validator("result", mode="before")
    @classmethod
    def receipt_uses_wire_timestamp(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("result must be an object")
        published_at = value.get("published_at")
        if (
            not isinstance(published_at, str)
            or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z",
                published_at,
            )
            is None
        ):
            raise ValueError("published_at must be a UTC RFC3339 string using Z")
        return value

    @field_validator("request_id")
    @classmethod
    def request_id_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request_id must not be blank")
        return value


class MockPublicationRecord(PublicationModel):
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: ReportPublicationRequest
    receipt: ReportPublicationReceipt


def _result(value: ReportResult) -> PublicationResult:
    return PublicationResult(code=value.code, label=value.label)


def _confidence(value: ReportConfidence) -> PublicationConfidence:
    catalog = {"低": "low", "中": "medium", "高": "high"}
    try:
        code = catalog[value.label]
    except KeyError as exc:
        raise ValueError(f"unsupported Report confidence label: {value.label}") from exc
    return PublicationConfidence(code=code, label=value.label)


def _time_window(value: str) -> PublicationTimeWindow:
    catalog = {
        "短期": "short",
        "中期": "medium",
        "长期": "long",
        "短期–中期": "short_medium",
        "短期–长期": "short_long",
        "中期–长期": "medium_long",
        "短期–中期–长期": "short_medium_long",
        "后续周期": "follow_up",
    }
    try:
        code = catalog[value]
    except KeyError as exc:
        raise ValueError(f"unsupported Report time window: {value}") from exc
    return PublicationTimeWindow(code=code, label=value)


def _basis_and_status(
    value: ReportNature,
) -> tuple[PublicationConclusionBasis, PublicationValidationStatus]:
    if value.code == "direct_evidence":
        return (
            PublicationConclusionBasis(code="direct_evidence", label="直接证据"),
            PublicationValidationStatus(code="confirmed", label="已确认"),
        )
    if value.code == "reasoning_hypothesis":
        return (
            PublicationConclusionBasis(code="reasoning_hypothesis", label="推理假设"),
            PublicationValidationStatus(code="pending_validation", label="待验证"),
        )
    return (
        PublicationConclusionBasis(code="no_directional_conclusion", label="无方向性结论"),
        PublicationValidationStatus(code="pending_validation", label="待验证"),
    )


def _evidence_refs(
    values: list[ReportEvidenceReference],
    *,
    role: Literal["direct_support", "reasoning_support", "summary_support"],
) -> list[PublicationEvidenceReference]:
    labels = {"direct_support": "直接依据", "reasoning_support": "推导依据", "summary_support": "核心依据"}
    publication_role = PublicationEvidenceRole(code=role, label=labels[role])
    return [PublicationEvidenceReference(evidence_id=value.evidence_id, role=publication_role) for value in values]


def _anchor(value: ReportAnchor) -> PublicationAnchor:
    basis, status = _basis_and_status(value.nature)
    return PublicationAnchor(
        local_key=value.key,
        name=value.name,
        current_state=value.current_state,
        result=_result(value.result),
        conclusion_basis=basis,
        validation_status=status,
        reasoning=value.reasoning,
        time_window=_time_window(value.time_window),
        confidence=_confidence(value.confidence),
        evidence_refs=_evidence_refs(value.evidence_refs, role="direct_support"),
    )


def _reasoning_step(value: ReportReasoningStep) -> PublicationReasoningStep:
    return PublicationReasoningStep(
        local_key=value.key,
        input=value.input,
        mechanism=value.mechanism,
        output=value.output,
        confidence=_confidence(value.confidence),
        evidence_refs=_evidence_refs(value.evidence_refs, role="reasoning_support"),
    )


def _target_name_index(report: InvestmentReportArtifact) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for anchor in report.content.macroeconomics.anchors:
        result[("anchor", anchor.key)] = anchor.name
    for chain in report.content.industry_chains:
        result[("industry_chain", chain.key)] = chain.name
        for node in chain.nodes:
            result[("industry_chain_node", node.key)] = node.name
    return result


def _transmission_path(
    value: ReportTransmissionPath,
    *,
    allowed_types: set[str],
    target_names: dict[tuple[str, str], str],
) -> PublicationTransmissionPath | None:
    target_types = {
        "anchor": PublicationTargetType(code="macro_anchor", label="宏观经济锚点"),
        "industry_chain": PublicationTargetType(code="industry_chain", label="产业链"),
        "industry_chain_node": PublicationTargetType(code="industry_chain_node", label="产业链节点"),
    }
    targets = [
        PublicationTransmissionTarget(
            target_type=target_types[target.ref.type],
            target_local_key=target.ref.key,
            target_name=target_names.get((target.ref.type, target.ref.key), target.label),
            result=_result(target.result),
        )
        for target in value.target_refs
        if target.ref.type in allowed_types
    ]
    if not targets:
        return None
    kind = (
        PublicationTransmissionKind(code="same_source_signal", label="同源信号")
        if value.relation_nature == "同源信号"
        else PublicationTransmissionKind(code="cross_layer_reasoning", label="跨层推理")
    )
    return PublicationTransmissionPath(
        local_key=value.key,
        source_conclusion=value.source_conclusion,
        targets=targets,
        transmission_logic=value.logic,
        transmission_kind=kind,
        confidence=_confidence(value.confidence),
    )


def _transmission_group(
    layer: ReportLayer,
    *,
    allowed_types: set[str],
    target_names: dict[tuple[str, str], str],
) -> PublicationTransmissionGroup:
    paths = [
        mapped
        for path in layer.downward_transmission.published_paths
        if (mapped := _transmission_path(path, allowed_types=allowed_types, target_names=target_names)) is not None
    ]
    return PublicationTransmissionGroup(summary=layer.downward_transmission.summary, paths=paths)


def _layer_base(value: ReportLayer) -> dict[str, object]:
    return {
        "local_key": value.key,
        "title": value.title,
        "conclusion": value.conclusion,
        "result": _result(value.result),
        "time_window": _time_window(value.time_window),
        "confidence": _confidence(value.confidence),
        "affected_anchors": [_anchor(item) for item in value.anchors],
        "reasoning_steps": [_reasoning_step(item) for item in value.reasoning_steps],
        "uncertainty": PublicationLayerUncertainty(
            counterevidence=value.uncertainty.counterevidence,
            evidence_gap=value.uncertainty.evidence_gap,
            boundary=value.uncertainty.boundary,
            reversal_condition=value.uncertainty.reversal_condition,
        ),
        "evidence_refs": _evidence_refs(value.evidence_refs, role="summary_support"),
    }


def _node(value: ReportIndustryChainNode) -> PublicationIndustryChainNode:
    basis, status = _basis_and_status(value.nature)
    role: Literal["direct_support", "reasoning_support", "summary_support"] = (
        "direct_support" if value.nature.code == "direct_evidence" else "reasoning_support"
    )
    return PublicationIndustryChainNode(
        local_key=value.key,
        name=value.name,
        impact=value.impact,
        result=_result(value.result),
        conclusion_basis=basis,
        validation_status=status,
        reasoning=value.reasoning,
        time_window=_time_window(value.time_window),
        confidence=_confidence(value.confidence),
        evidence_refs=_evidence_refs(value.evidence_refs, role=role),
    )


def _chain(value: ReportIndustryChain) -> PublicationIndustryChain:
    return PublicationIndustryChain(
        local_key=value.key,
        name=value.name,
        conclusion=value.conclusion,
        result=_result(value.result),
        time_window=_time_window(value.time_window),
        confidence=_confidence(value.confidence),
        path_summary=value.path_summary,
        accepted_hypothesis_summary=value.accepted_hypothesis_summary,
        nodes=[_node(item) for item in value.nodes],
        edges=[
            PublicationIndustryChainEdge(
                from_node_local_key=item.from_node_key,
                to_node_local_key=item.to_node_key,
                relation_label=item.relation_label,
            )
            for item in value.edges
        ],
        uncertainty=PublicationChainUncertainty(
            counterevidence_and_gap=value.uncertainty.counterevidence_and_gap,
            stop_condition=value.uncertainty.stop_condition,
        ),
        evidence_refs=_evidence_refs(value.evidence_refs, role="summary_support"),
    )


def build_report_publication(report: InvestmentReportArtifact) -> ReportPublicationRequest:
    """Project one immutable internal Report into the frozen external publication shape."""

    target_names = _target_name_index(report)
    geo = report.content.geopolitics
    macro = report.content.macroeconomics
    return ReportPublicationRequest(
        publisher_report_id=report.source_report_id,
        report=PublicationReport(
            generated_at=report.content.generated_at,
            timezone=report.content.timezone,
            geopolitics=PublicationGeopoliticalLayer(
                **_layer_base(geo),
                downward_transmission=PublicationGeopoliticalDownwardTransmission(
                    to_macroeconomics=_transmission_group(
                        geo,
                        allowed_types={"anchor"},
                        target_names=target_names,
                    ),
                    to_industry_chains=_transmission_group(
                        geo,
                        allowed_types={"industry_chain", "industry_chain_node"},
                        target_names=target_names,
                    ),
                ),
            ),
            macroeconomics=PublicationMacroeconomicLayer(
                **_layer_base(macro),
                downward_transmission=PublicationMacroeconomicDownwardTransmission(
                    to_industry_chains=_transmission_group(
                        macro,
                        allowed_types={"industry_chain", "industry_chain_node"},
                        target_names=target_names,
                    )
                ),
            ),
            industry_chains=[_chain(item) for item in report.content.industry_chains],
        ),
    )


def _canonical_sha256(request: ReportPublicationRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


REPORT_PUBLICATIONS_PATH = "/api/data/v1/report-publications"
REPORT_PUBLICATION_TIMEOUT_SECONDS = 20.0
REPORT_PUBLICATION_TOTAL_TIMEOUT_SECONDS = 45.0
REPORT_PUBLICATION_MAX_ATTEMPTS = 2


class DataServiceReportPublisher:
    """Authenticated, bounded publisher for the versioned Data Report API."""

    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_seconds: float = REPORT_PUBLICATION_TIMEOUT_SECONDS,
        total_timeout_seconds: float = REPORT_PUBLICATION_TOTAL_TIMEOUT_SECONDS,
        max_attempts: int = REPORT_PUBLICATION_MAX_ATTEMPTS,
        retry_delay_seconds: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DATA_SERVICE_BASE_URL is invalid")
        token = service_token.strip()
        if not token:
            raise ValueError("DATA_SERVICE_TOKEN is not configured")
        if any(ord(character) < 0x21 or ord(character) > 0x7E for character in token):
            raise ValueError("DATA_SERVICE_TOKEN contains invalid characters")
        if timeout_seconds <= 0:
            raise ValueError("Report publication timeout must be positive")
        if total_timeout_seconds <= 0:
            raise ValueError("Report publication total timeout must be positive")
        if max_attempts < 1:
            raise ValueError("Report publication max attempts must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("Report publication retry delay must not be negative")
        self._url = f"{base_url.rstrip('/')}{REPORT_PUBLICATIONS_PATH}"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._request_timeout_seconds = timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._transport = transport

    async def publish(self, request: ReportPublicationRequest) -> ReportPublicationReceipt:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._total_timeout_seconds
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                return await self._publish_with_retries(request, deadline)
        except TimeoutError:
            raise ReportPublicationUnavailable(
                f"Data Service Report publication exceeded the {self._total_timeout_seconds:g}s total timeout"
            ) from None

    async def _publish_with_retries(
        self,
        request: ReportPublicationRequest,
        deadline: float,
    ) -> ReportPublicationReceipt:
        payload = request.model_dump(mode="json")
        for attempt in range(1, self._max_attempts + 1):
            try:
                remaining_seconds = max(deadline - asyncio.get_running_loop().time(), 0.001)
                attempt_timeout = min(self._request_timeout_seconds, remaining_seconds)
                async with asyncio.timeout(attempt_timeout):
                    async with httpx.AsyncClient(
                        timeout=attempt_timeout,
                        headers=self._headers,
                        transport=self._transport,
                    ) as client:
                        response = await client.post(self._url, json=payload)
            except TimeoutError:
                if attempt < self._max_attempts:
                    await self._wait_before_retry(attempt)
                    continue
                raise ReportPublicationUnavailable(
                    f"Data Service Report publication timed out after {attempt} attempts"
                ) from None
            except httpx.RequestError:
                if attempt < self._max_attempts:
                    await self._wait_before_retry(attempt)
                    continue
                raise ReportPublicationUnavailable(
                    f"Data Service Report publication failed after {attempt} attempts"
                ) from None

            if response.status_code in {200, 201}:
                try:
                    envelope = ReportPublicationEnvelope.model_validate(response.json())
                except (TypeError, ValueError):
                    raise ReportPublicationUnavailable(
                        "Data Service Report publication returned an invalid success envelope"
                    ) from None
                if (response.status_code == 200) != envelope.result.replayed:
                    raise ReportPublicationUnavailable(
                        "Data Service Report publication returned inconsistent replay status"
                    )
                return envelope.result

            if response.status_code == 409:
                raise ReportPublicationConflict(
                    "publisher_report_id already exists with divergent Report publication content"
                )
            retryable_status = response.status_code in {408, 425, 429} or 500 <= response.status_code < 600
            if 400 <= response.status_code < 500 and not retryable_status:
                raise ReportPublicationRejected(
                    f"Data Service rejected Report publication with HTTP {response.status_code}"
                )
            if retryable_status and attempt < self._max_attempts:
                await self._wait_before_retry(attempt)
                continue
            raise ReportPublicationUnavailable(
                f"Data Service Report publication failed with HTTP {response.status_code} after {attempt} attempts"
            )
        raise AssertionError("Report publication retry loop did not return or raise")

    async def _wait_before_retry(self, attempt: int) -> None:
        if self._retry_delay_seconds:
            await asyncio.sleep(self._retry_delay_seconds * (2 ** (attempt - 1)))


class MockReportPublisher:
    """File-backed Data Service substitute with the production idempotency semantics."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (
            root or Path(os.getenv("INVESTMENT_REPORT_MOCK_PUBLICATION_ROOT", "data/investment/mock-publications"))
        ).resolve()

    def publication_path(self, publisher_report_id: str) -> Path:
        path = (self._root / f"{publisher_report_id}.json").resolve()
        if self._root != path and self._root not in path.parents:
            raise ValueError("Report publication path escapes its root")
        return path

    async def publish(self, request: ReportPublicationRequest) -> ReportPublicationReceipt:
        path = self.publication_path(request.publisher_report_id)
        content_sha256 = _canonical_sha256(request)
        receipt = ReportPublicationReceipt(
            report_id=f"RPT{uuid5(NAMESPACE_URL, request.publisher_report_id)}",
            published_at=datetime.now(UTC),
            replayed=False,
        )
        record = MockPublicationRecord(content_sha256=content_sha256, request=request, receipt=receipt)
        if _atomic_create_record(path, record):
            return receipt
        existing = MockPublicationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if existing.content_sha256 != content_sha256 or existing.request != request:
            raise ReportPublicationConflict(
                "publisher_report_id already exists with divergent Report publication content"
            )
        return existing.receipt.model_copy(update={"replayed": True})


def _atomic_create_record(path: Path, record: MockPublicationRecord) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)
    return True
