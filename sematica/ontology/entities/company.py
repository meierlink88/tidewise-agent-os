"""Company entities and provenance-separated outbound Graphiti relations."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import Field, field_validator

from sematica.ontology.entities.base import TidewiseEntity, TidewiseEntityLink
from sematica.ontology.enums import CompanyOwnershipType, CompanyStatus

COMPANY_ID_PATTERN = r"^COM[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
COMPANY_INDUSTRY_LINK_ID_PATTERN = r"^CIL[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
COUNTRY_ID_PATTERN = r"^COU[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
INDUSTRY_ID_PATTERN = r"^IND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
CHAIN_NODE_ID_PATTERN = r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
INDUSTRY_CHAIN_ID_PATTERN = r"^ICH[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
COMPANY_PROJECTION_OWNER = "tidewise-agentos/company-projection/v1"


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


class Company(TidewiseEntity):
    """Data-owned enterprise identity; never an issued Security, product or contextual Event entity."""

    data_object_id: str | None = Field(
        default=None,
        pattern=COMPANY_ID_PATTERN,
        description="Tidewise Data canonical Company ID; it must never be inferred or invented.",
    )
    projection_owner: Literal["tidewise-agentos/company-projection/v1"] = "tidewise-agentos/company-projection/v1"
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        description="Stable Company business code owned by Tidewise Data.",
    )
    name_en: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list)
    registration_country_id: str | None = Field(
        default=None,
        pattern=COUNTRY_ID_PATTERN,
        description="Canonical registration Country retained as an attribute; this projection creates no Country edge.",
    )
    operating_area: str | None = Field(default=None, min_length=1)
    headquarters_city: str | None = Field(default=None, min_length=1, max_length=100)
    founding_date: date | None = None
    ipo_date: date | None = None
    legal_form: str | None = Field(default=None, min_length=1, max_length=64)
    ownership_type: CompanyOwnershipType | None = None
    strategic_positioning: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    status: CompanyStatus | None = None
    source_record_fingerprint: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
        description="Canonical hash of the Data Company fields represented by this node.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Authoritative Company record update time from Tidewise Data.",
    )

    @field_validator("updated_at")
    @classmethod
    def updated_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and not _is_utc(value):
            raise ValueError("Company updated_at must be explicit UTC")
        return value


class CompanyBelongsToIndustry(TidewiseEntityLink):
    """Formal Company-to-Industry fact owned by one Data CompanyIndustryLink record."""

    data_object_id: str = Field(
        pattern=COMPANY_INDUSTRY_LINK_ID_PATTERN,
        description="Canonical CompanyIndustryLink ID from Tidewise Data.",
    )
    projection_owner: Literal["tidewise-agentos/company-projection/v1"] = "tidewise-agentos/company-projection/v1"
    source_company_id: str = Field(pattern=COMPANY_ID_PATTERN)
    target_data_object_id: str = Field(pattern=INDUSTRY_ID_PATTERN)
    projection_fingerprint: str = Field(pattern=SHA256_PATTERN)
    source_record_created_at: datetime = Field(
        description="Creation time of the Data CompanyIndustryLink, separate from Graphiti relationship creation time.",
    )

    @field_validator("source_record_created_at")
    @classmethod
    def source_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("CompanyIndustryLink source timestamp must be explicit UTC")
        return value


class _ModelInferredCompanyLink(TidewiseEntityLink):
    """Auditable model decision fields shared by Company inferred relations."""

    derivation_type: Literal["MODEL_INFERRED"]
    projection_owner: Literal["tidewise-agentos/company-projection/v1"] = "tidewise-agentos/company-projection/v1"
    decision_id: str = Field(pattern=SHA256_PATTERN)
    source_company_id: str = Field(pattern=COMPANY_ID_PATTERN)
    target_data_object_id: str = Field(pattern=rf"(?:{INDUSTRY_ID_PATTERN[1:-1]}|{CHAIN_NODE_ID_PATTERN[1:-1]})")
    projection_fingerprint: str = Field(pattern=SHA256_PATTERN)
    confidence: Literal["MEDIUM", "HIGH"]
    rationale: str = Field(min_length=1, max_length=1000)
    source_company_fingerprint: str = Field(pattern=SHA256_PATTERN)
    target_catalog_fingerprint: str = Field(pattern=SHA256_PATTERN)
    model_id: str = Field(min_length=1, max_length=200)
    prompt_contract_version: str = Field(min_length=1, max_length=128)
    ontology_version: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    supporting_company_fields: list[
        Literal["name", "name_en", "legal_name", "aliases", "strategic_positioning", "description"]
    ] = Field(min_length=1, max_length=6)
    source_industry_ids: list[str]
    industry_chain_ids: list[str] = Field(default_factory=list)
    decided_at: datetime

    @field_validator("rationale", "model_id", "prompt_contract_version", "ontology_version", "policy_version")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("inference provenance text must not be blank")
        return value

    @field_validator("source_industry_ids")
    @classmethod
    def source_industries_must_be_canonical_and_unique(cls, values: list[str]) -> list[str]:
        if any(re.fullmatch(INDUSTRY_ID_PATTERN, value) is None for value in values):
            raise ValueError("source Industry IDs must be canonical")
        if len(values) != len(set(values)):
            raise ValueError("source Industry IDs must be unique")
        return values

    @field_validator("industry_chain_ids")
    @classmethod
    def industry_chains_must_be_canonical_and_unique(cls, values: list[str]) -> list[str]:
        if any(re.fullmatch(INDUSTRY_CHAIN_ID_PATTERN, value) is None for value in values):
            raise ValueError("IndustryChain IDs must be canonical")
        if len(values) != len(set(values)):
            raise ValueError("IndustryChain IDs must be unique")
        return values

    @field_validator("decided_at")
    @classmethod
    def decision_timestamp_must_be_utc(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("inference decided_at must be explicit UTC")
        return value


class CompanyOperatesInIndustry(_ModelInferredCompanyLink):
    """Model-inferred Company operating relation to an existing canonical Industry."""

    target_data_object_id: str = Field(pattern=INDUSTRY_ID_PATTERN)


class CompanyParticipatesInChainNode(_ModelInferredCompanyLink):
    """Model-inferred Company participation in an existing canonical ChainNode."""

    target_data_object_id: str = Field(pattern=CHAIN_NODE_ID_PATTERN)
    source_industry_ids: list[str] = Field(min_length=1)


ENTITY_TYPES = {"Company": Company}
EDGE_TYPES = {
    "CompanyBelongsToIndustry": CompanyBelongsToIndustry,
    "CompanyOperatesInIndustry": CompanyOperatesInIndustry,
    "CompanyParticipatesInChainNode": CompanyParticipatesInChainNode,
}
EDGE_TYPE_MAP = {
    ("Company", "Industry"): ["CompanyBelongsToIndustry", "CompanyOperatesInIndustry"],
    ("Company", "ChainNode"): ["CompanyParticipatesInChainNode"],
}
