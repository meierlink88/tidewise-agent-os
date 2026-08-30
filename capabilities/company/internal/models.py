"""Strict contracts for bounded Company target selection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
COM_PATTERN = r"^COM[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
IND_PATTERN = r"^IND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
ICH_PATTERN = r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
CND_PATTERN = r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
TARGET_PATTERN = rf"(?:{IND_PATTERN[1:-1]}|{CND_PATTERN[1:-1]})"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be explicit UTC")
    return value


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Confidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class DecisionStatus(StrEnum):
    MAPPED = "MAPPED"
    NO_CANDIDATE = "NO_CANDIDATE"
    NO_MATCH = "NO_MATCH"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"


SupportingField = Literal[
    "name",
    "name_en",
    "legal_name",
    "aliases",
    "strategic_positioning",
    "description",
]


class CompanySubject(FrozenModel):
    """One Company input whose projection facts have already passed the Data DTO gate."""

    input_index: int = Field(ge=0)
    company_id: str = Field(pattern=COM_PATTERN)
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    name_en: str | None = Field(default=None, min_length=1, max_length=500)
    legal_name: str | None = Field(default=None, min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    registration_country_id: str | None = None
    strategic_positioning: str | None = Field(default=None, min_length=1, max_length=4000)
    description: str | None = Field(default=None, min_length=1, max_length=10000)
    source_updated_at: datetime

    @field_validator("aliases")
    @classmethod
    def aliases_are_nonblank_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("aliases must not contain blank values")
        if len(values) != len(set(values)):
            raise ValueError("aliases must be unique")
        return values

    @field_validator("source_updated_at")
    @classmethod
    def source_time_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "source_updated_at")

    def fingerprint(self) -> str:
        return _canonical_hash(self.model_dump(mode="json", exclude={"input_index"}))


class CanonicalIndustry(FrozenModel):
    industry_id: str = Field(pattern=IND_PATTERN)
    name: str = Field(min_length=1, max_length=500)
    definition: str = Field(min_length=1, max_length=10000)
    parent_id: str | None = Field(default=None, pattern=IND_PATTERN)


class CanonicalIndustryChain(FrozenModel):
    industry_chain_id: str = Field(pattern=ICH_PATTERN)
    name: str = Field(min_length=1, max_length=500)


class CanonicalChainNode(FrozenModel):
    chain_node_id: str = Field(pattern=CND_PATTERN)
    name: str = Field(min_length=1, max_length=500)
    definition: str = Field(min_length=1, max_length=10000)


class IndustryChainMapping(FrozenModel):
    industry_chain_id: str = Field(pattern=ICH_PATTERN)
    industry_id: str = Field(pattern=IND_PATTERN)


class ChainMembership(FrozenModel):
    industry_chain_id: str = Field(pattern=ICH_PATTERN)
    chain_node_id: str = Field(pattern=CND_PATTERN)


class TargetCatalog(FrozenModel):
    """Frozen canonical target catalog and only the topology allowed for inference."""

    industries: list[CanonicalIndustry]
    industry_chains: list[CanonicalIndustryChain]
    chain_nodes: list[CanonicalChainNode]
    industry_chain_mappings: list[IndustryChainMapping]
    chain_memberships: list[ChainMembership]

    @model_validator(mode="after")
    def topology_endpoints_are_canonical(self) -> TargetCatalog:
        industry_ids = [item.industry_id for item in self.industries]
        chain_ids = [item.industry_chain_id for item in self.industry_chains]
        node_ids = [item.chain_node_id for item in self.chain_nodes]
        for label, ids in (("Industry", industry_ids), ("IndustryChain", chain_ids), ("ChainNode", node_ids)):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} ID")
        known_industries = set(industry_ids)
        known_chains = set(chain_ids)
        known_nodes = set(node_ids)
        for industry in self.industries:
            if industry.parent_id is not None and industry.parent_id not in known_industries:
                raise ValueError(f"Industry {industry.industry_id} has unknown parent")
            if industry.parent_id == industry.industry_id:
                raise ValueError(f"Industry {industry.industry_id} is its own parent")
        mapping_keys: set[tuple[str, str]] = set()
        for mapping in self.industry_chain_mappings:
            if mapping.industry_chain_id not in known_chains:
                raise ValueError(f"mapping references unknown IndustryChain {mapping.industry_chain_id}")
            if mapping.industry_id not in known_industries:
                raise ValueError(f"mapping references unknown Industry {mapping.industry_id}")
            key = (mapping.industry_chain_id, mapping.industry_id)
            if key in mapping_keys:
                raise ValueError("duplicate IndustryChain mapping endpoints")
            mapping_keys.add(key)
        membership_keys: set[tuple[str, str]] = set()
        for membership in self.chain_memberships:
            if membership.industry_chain_id not in known_chains:
                raise ValueError(f"membership references unknown IndustryChain {membership.industry_chain_id}")
            if membership.chain_node_id not in known_nodes:
                raise ValueError(f"membership references unknown ChainNode {membership.chain_node_id}")
            key = (membership.industry_chain_id, membership.chain_node_id)
            if key in membership_keys:
                raise ValueError("duplicate ChainNode membership endpoints")
            membership_keys.add(key)
        parents = {item.industry_id: item.parent_id for item in self.industries}
        for industry_id in parents:
            observed: set[str] = set()
            current: str | None = industry_id
            while current is not None:
                if current in observed:
                    raise ValueError("Industry hierarchy contains a cycle")
                observed.add(current)
                current = parents[current]
        return self

    def fingerprint(self) -> str:
        return _canonical_hash(self.model_dump(mode="json"))


class CandidateChoice(FrozenModel):
    """One supplied target option. The model may return only its short key."""

    key: str = Field(pattern=r"^[IN][1-9][0-9]*$")
    target_id: str = Field(pattern=TARGET_PATTERN)
    name: str = Field(min_length=1, max_length=500)
    definition: str = Field(min_length=1, max_length=10000)
    context: list[str] = Field(default_factory=list, max_length=30)
    source_industry_ids: list[str] = Field(default_factory=list)
    industry_chain_ids: list[str] = Field(default_factory=list)

    @field_validator("source_industry_ids")
    @classmethod
    def valid_source_industry_ids(cls, values: list[str]) -> list[str]:
        if any(__import__("re").fullmatch(IND_PATTERN, value) is None for value in values):
            raise ValueError("source_industry_ids contains invalid Industry ID")
        if len(values) != len(set(values)):
            raise ValueError("source_industry_ids must be unique")
        return values

    @field_validator("industry_chain_ids")
    @classmethod
    def valid_industry_chain_ids(cls, values: list[str]) -> list[str]:
        if any(__import__("re").fullmatch(ICH_PATTERN, value) is None for value in values):
            raise ValueError("industry_chain_ids contains invalid IndustryChain ID")
        if len(values) != len(set(values)):
            raise ValueError("industry_chain_ids must be unique")
        return values


class ModelTargetSelection(FrozenModel):
    candidate_key: str = Field(pattern=r"^[IN][1-9][0-9]*$")
    confidence: Confidence
    rationale: str = Field(min_length=1, max_length=800)
    supporting_company_fields: list[SupportingField] = Field(min_length=1, max_length=6)

    @field_validator("supporting_company_fields")
    @classmethod
    def supporting_fields_are_unique(cls, values: list[SupportingField]) -> list[SupportingField]:
        if len(values) != len(set(values)):
            raise ValueError("supporting_company_fields must be unique")
        return values


class ModelSelectionItem(FrozenModel):
    input_index: int = Field(ge=0)
    selections: list[ModelTargetSelection] = Field(default_factory=list, max_length=8)
    no_match_reason: str | None = Field(default=None, min_length=1, max_length=800)

    @model_validator(mode="after")
    def match_state_is_unambiguous(self) -> ModelSelectionItem:
        if self.selections and self.no_match_reason is not None:
            raise ValueError("no_match_reason is only valid when selections is empty")
        if not self.selections and self.no_match_reason is None:
            raise ValueError("empty selections require no_match_reason")
        keys = [item.candidate_key for item in self.selections]
        if len(keys) != len(set(keys)):
            raise ValueError("selected candidate keys must be unique")
        return self


class ModelSelectionResponse(FrozenModel):
    items: list[ModelSelectionItem]


class ResolvedTarget(FrozenModel):
    target_id: str = Field(pattern=TARGET_PATTERN)
    confidence: Confidence
    rationale: str = Field(min_length=1, max_length=800)
    supporting_company_fields: list[SupportingField] = Field(min_length=1, max_length=6)
    source_industry_ids: list[str] = Field(default_factory=list)
    industry_chain_ids: list[str] = Field(default_factory=list)


class StageDecision(FrozenModel):
    status: DecisionStatus
    accepted_targets: list[ResolvedTarget]
    rejected_targets: list[ResolvedTarget]
    reason: str | None = Field(default=None, min_length=1, max_length=800)

    @model_validator(mode="after")
    def status_matches_targets(self) -> StageDecision:
        if self.status == DecisionStatus.MAPPED and not self.accepted_targets:
            raise ValueError("MAPPED requires an accepted target")
        if self.status != DecisionStatus.MAPPED and self.accepted_targets:
            raise ValueError("non-MAPPED decision cannot have accepted targets")
        return self


class ProjectionRunManifest(FrozenModel):
    """Frozen inputs and executable policy for one complete projection run."""

    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    company_snapshot_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_catalog_fingerprint: str = Field(pattern=SHA256_PATTERN)
    company_ids: list[str] = Field(min_length=1)
    ontology_version: str = Field(min_length=1, max_length=100)
    policy_version: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_contract_version: str = Field(min_length=1, max_length=100)
    max_chain_candidates: int = Field(default=80, ge=1, le=200)
    created_at: datetime

    @field_validator("company_ids")
    @classmethod
    def company_ids_are_canonical_and_unique(cls, values: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(COM_PATTERN, value) is None for value in values):
            raise ValueError("company_ids contains invalid Company ID")
        if len(values) != len(set(values)):
            raise ValueError("company_ids must be unique")
        return values

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "created_at")

    def fingerprint(self) -> str:
        return _canonical_hash(self.model_dump(mode="json", exclude={"created_at"}))


class CandidateSetAudit(FrozenModel):
    """Exact target IDs offered behind short keys for one Company decision."""

    root_industry_candidate_ids: list[str] = Field(min_length=1)
    selected_root_industry_ids: list[str] = Field(default_factory=list, max_length=3)
    industry_candidate_ids: list[str] = Field(default_factory=list)
    chain_node_candidate_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("root_industry_candidate_ids", "selected_root_industry_ids", "industry_candidate_ids")
    @classmethod
    def industry_ids_are_canonical_and_unique(cls, values: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(IND_PATTERN, value) is None for value in values):
            raise ValueError("candidate audit contains an invalid Industry ID")
        if len(values) != len(set(values)):
            raise ValueError("candidate audit contains duplicate Industry IDs")
        return values

    @field_validator("chain_node_candidate_ids")
    @classmethod
    def chain_node_ids_are_canonical_and_unique(cls, values: list[str]) -> list[str]:
        import re

        if any(re.fullmatch(CND_PATTERN, value) is None for value in values):
            raise ValueError("candidate audit contains an invalid ChainNode ID")
        if len(values) != len(set(values)):
            raise ValueError("candidate audit contains duplicate ChainNode IDs")
        return values

    @model_validator(mode="after")
    def selected_roots_define_the_detailed_scope(self) -> CandidateSetAudit:
        if not set(self.selected_root_industry_ids).issubset(self.root_industry_candidate_ids):
            raise ValueError("selected root Industry was not offered")
        if self.selected_root_industry_ids and not self.industry_candidate_ids:
            raise ValueError("selected root Industries require a detailed Industry candidate set")
        if not self.selected_root_industry_ids and self.industry_candidate_ids:
            raise ValueError("detailed Industry candidates require a selected root Industry")
        return self


class CompanyInferenceDecision(FrozenModel):
    decision_id: str = Field(pattern=SHA256_PATTERN)
    company_id: str = Field(pattern=COM_PATTERN)
    input_index: int = Field(ge=0)
    status: DecisionStatus
    industry: StageDecision
    chain_node: StageDecision
    candidates: CandidateSetAudit
    source_company_fingerprint: str = Field(pattern=SHA256_PATTERN)
    snapshot_id: str = Field(pattern=SHA256_PATTERN)
    target_catalog_fingerprint: str = Field(pattern=SHA256_PATTERN)
    ontology_version: str
    policy_version: str
    model_id: str
    prompt_contract_version: str
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def decided_at_is_utc(cls, value: datetime) -> datetime:
        return _utc(value, "decided_at")

    @model_validator(mode="after")
    def selected_targets_were_in_the_offered_candidate_sets(self) -> CompanyInferenceDecision:
        offered_industries = (
            self.candidates.industry_candidate_ids
            if self.candidates.selected_root_industry_ids
            else self.candidates.root_industry_candidate_ids
        )
        selected_industries = {
            target.target_id for target in [*self.industry.accepted_targets, *self.industry.rejected_targets]
        }
        if not selected_industries.issubset(offered_industries):
            raise ValueError("Industry decision contains a target that was not offered")
        selected_chain_nodes = {
            target.target_id for target in [*self.chain_node.accepted_targets, *self.chain_node.rejected_targets]
        }
        if not selected_chain_nodes.issubset(self.candidates.chain_node_candidate_ids):
            raise ValueError("ChainNode decision contains a target that was not offered")
        return self


__all__ = [
    "CND_PATTERN",
    "COM_PATTERN",
    "ICH_PATTERN",
    "IND_PATTERN",
    "CandidateChoice",
    "CandidateSetAudit",
    "CanonicalChainNode",
    "CanonicalIndustry",
    "CanonicalIndustryChain",
    "ChainMembership",
    "CompanyInferenceDecision",
    "CompanySubject",
    "Confidence",
    "DecisionStatus",
    "IndustryChainMapping",
    "ModelSelectionItem",
    "ModelSelectionResponse",
    "ProjectionRunManifest",
    "ResolvedTarget",
    "StageDecision",
    "TargetCatalog",
    "_canonical_hash",
]
