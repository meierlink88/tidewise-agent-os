"""Strict Company snapshot consumption and deterministic Graphiti projection data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Literal

import httpx
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from capabilities.company import (
    CompanyInferenceDecision,
    CompanySubject,
    Confidence,
    ProjectionRunManifest,
    TargetCatalog,
    validate_decision_candidate_scope,
)
from sematica.graphiti.company import load_company_target_catalog
from sematica.ontology import (
    EDGE_TYPE_MAP,
    Company,
    CompanyBelongsToIndustry,
    CompanyOperatesInIndustry,
    CompanyParticipatesInChainNode,
)
from sematica.ontology.entities.company import (
    COMPANY_ID_PATTERN,
    COMPANY_INDUSTRY_LINK_ID_PATTERN,
    COMPANY_PROJECTION_OWNER,
    COUNTRY_ID_PATTERN,
    INDUSTRY_ID_PATTERN,
)
from sematica.ontology.enums import CompanyOwnershipType, CompanyStatus
from sematica.projection.authoritative_writer import GROUP_ID, edge_uuid, node_uuid, write_projection
from sematica.projection.runtime import ProjectionError, RuntimeConfig

COMPANIES_PATH = "/api/data/v1/entities/companies"
COMPANY_PROJECTION_SCHEMA_VERSION = "company-projection-snapshot.v1"
PAGE_SIZE = 100
SNAPSHOT_ID_PATTERN = r"^[0-9a-f]{64}$"
CHAIN_NODE_ID_PATTERN = r"^CND[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
UTC_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TargetLabel = Literal["Industry", "ChainNode"]
OWNED_COMPANY_EDGE_NAMES = frozenset(
    {"CompanyBelongsToIndustry", "CompanyOperatesInIndustry", "CompanyParticipatesInChainNode"}
)
COMPANY_WRITE_BATCH_SIZE = 100


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def company_source_fingerprint(company: DataCompanyDTO) -> str:
    """Hash only fields represented by the Company node, excluding separate link facts."""

    return _canonical_sha256(company.model_dump(mode="json", exclude={"industry_links"}))


def company_subject(company: DataCompanyDTO, input_index: int) -> CompanySubject:
    """Expose only model-relevant Company fields behind a stable input index."""

    return CompanySubject(
        input_index=input_index,
        company_id=company.id,
        code=company.code,
        name=company.name,
        name_en=company.name_en,
        legal_name=company.legal_name,
        aliases=company.aliases,
        registration_country_id=company.registration_country_id,
        strategic_positioning=company.strategic_positioning,
        description=company.description,
        source_updated_at=company.updated_at,
    )


def _require_utc_rfc3339(value: object) -> object:
    if not isinstance(value, str) or UTC_RFC3339_PATTERN.fullmatch(value) is None:
        raise ValueError("timestamp must be an explicit UTC RFC3339 string")
    return value


def _require_calendar_date(value: object) -> object:
    if value is not None and (not isinstance(value, str) or DATE_PATTERN.fullmatch(value) is None):
        raise ValueError("date must use YYYY-MM-DD")
    return value


class DataCompanyIndustryLinkDTO(BaseModel):
    """One formal CompanyIndustryLink owned by Tidewise Data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=COMPANY_INDUSTRY_LINK_ID_PATTERN)
    company_id: str = Field(pattern=COMPANY_ID_PATTERN)
    industry_id: str = Field(pattern=INDUSTRY_ID_PATTERN)
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def created_at_must_use_the_wire_format(cls, value: object) -> object:
        return _require_utc_rfc3339(value)

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("CompanyIndustryLink created_at must be explicit UTC")
        return value


class DataCompanyDTO(BaseModel):
    """Frozen consumer contract for one Data-owned Company fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=COMPANY_ID_PATTERN)
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    name_en: str | None = Field(max_length=200)
    legal_name: str | None = Field(max_length=300)
    aliases: list[str]
    registration_country_id: str | None = Field(pattern=COUNTRY_ID_PATTERN)
    operating_area: str | None
    headquarters_city: str | None = Field(max_length=100)
    founding_date: date | None
    ipo_date: date | None
    legal_form: str | None = Field(max_length=64)
    ownership_type: CompanyOwnershipType | None
    strategic_positioning: str | None
    description: str | None
    status: CompanyStatus
    created_at: datetime
    updated_at: datetime
    industry_links: list[DataCompanyIndustryLinkDTO]

    @field_validator("founding_date", "ipo_date", mode="before")
    @classmethod
    def dates_must_use_the_wire_format(cls, value: object) -> object:
        return _require_calendar_date(value)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def timestamps_must_use_the_wire_format(cls, value: object) -> object:
        return _require_utc_rfc3339(value)

    @field_validator(
        "code",
        "name",
        "name_en",
        "legal_name",
        "operating_area",
        "headquarters_city",
        "legal_form",
        "strategic_positioning",
        "description",
    )
    @classmethod
    def text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Company text must not be blank")
        return value

    @field_validator("aliases")
    @classmethod
    def aliases_must_be_nonblank_bounded_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Company aliases must be nonblank and contain at most 200 characters")
        if len(values) != len(set(values)):
            raise ValueError("Company aliases must be unique")
        return values

    @model_validator(mode="after")
    def dates_timestamps_and_links_must_be_consistent(self) -> DataCompanyDTO:
        if not _is_utc(self.created_at) or not _is_utc(self.updated_at):
            raise ValueError("Company timestamps must be explicit UTC")
        if self.updated_at < self.created_at:
            raise ValueError("Company updated_at precedes created_at")
        if self.founding_date is not None and self.ipo_date is not None and self.ipo_date < self.founding_date:
            raise ValueError("Company ipo_date precedes founding_date")

        link_ids: set[str] = set()
        industry_ids: set[str] = set()
        for link in self.industry_links:
            if link.company_id != self.id:
                raise ValueError("CompanyIndustryLink source does not match its containing Company")
            if link.id in link_ids:
                raise ValueError("duplicate CompanyIndustryLink ID in Company")
            if link.industry_id in industry_ids:
                raise ValueError("duplicate CompanyIndustryLink endpoints in Company")
            link_ids.add(link.id)
            industry_ids.add(link.industry_id)
        return self


class CompanyPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["company-projection-snapshot.v1"]
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    items: list[DataCompanyDTO]
    next_cursor: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("next_cursor")
    @classmethod
    def cursor_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Company cursor must not be blank")
        return value


class CompanyPageEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    result: CompanyPage

    @field_validator("request_id")
    @classmethod
    def request_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Company request_id must not be blank")
        return value


class CompanyFacts(BaseModel):
    """One complete immutable Company snapshot from the Data API."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["company-projection-snapshot.v1"]
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    companies: tuple[DataCompanyDTO, ...]


class CompanyPlan(BaseModel):
    """Deterministic Company node and formal Industry relationship projection data."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    schema_version: Literal["company-projection-snapshot.v1"]
    snapshot_id: str = Field(pattern=SNAPSHOT_ID_PATTERN)
    company_count: int
    formal_industry_relation_count: int
    nodes: tuple[EntityNode, ...]
    formal_industry_edges: tuple[EntityEdge, ...]
    formal_industry_ids: frozenset[str]

    def summary(self) -> dict[str, object]:
        return {
            "group_id": GROUP_ID,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "companies": self.company_count,
            "formal_company_industry_relations": self.formal_industry_relation_count,
        }


class CanonicalGraphTarget(BaseModel):
    """One already-existing canonical target accepted by graph preflight."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_object_id: str
    uuid: str
    name: str = Field(min_length=1)
    label: TargetLabel


async def _request_page(
    client: httpx.AsyncClient,
    *,
    url: str,
    cursor: str | None,
) -> httpx.Response:
    params: dict[str, str | int] = {"page_size": PAGE_SIZE}
    if cursor is not None:
        params["cursor"] = cursor
    for attempt in range(2):
        try:
            response = await client.get(url, params=params)
        except httpx.TransportError:
            if attempt == 0:
                await asyncio.sleep(0.05)
                continue
            raise
        if attempt == 0 and (response.status_code == 429 or response.status_code >= 500):
            await asyncio.sleep(0.05)
            continue
        return response
    raise AssertionError("unreachable Company API retry state")


def _validate_complete_snapshot(companies: tuple[DataCompanyDTO, ...]) -> None:
    company_ids: set[str] = set()
    company_codes: set[str] = set()
    link_ids: set[str] = set()
    endpoints: set[tuple[str, str]] = set()
    for company in companies:
        if company.id in company_ids:
            raise ProjectionError(f"duplicate Company ID: {company.id}")
        if company.code in company_codes:
            raise ProjectionError(f"duplicate Company code: {company.code}")
        company_ids.add(company.id)
        company_codes.add(company.code)
        for link in company.industry_links:
            if link.id in link_ids:
                raise ProjectionError(f"duplicate CompanyIndustryLink ID: {link.id}")
            endpoint = (link.company_id, link.industry_id)
            if endpoint in endpoints:
                raise ProjectionError(f"duplicate CompanyIndustryLink endpoints: {endpoint}")
            link_ids.add(link.id)
            endpoints.add(endpoint)


async def load_facts(
    config: RuntimeConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompanyFacts:
    """Load every Company page and reject any version, snapshot or identity drift."""

    base_url = str(config.tidewise_data_base_url).rstrip("/")
    headers = {"Authorization": f"Bearer {config.tidewise_data_service_token.get_secret_value()}"}
    companies: list[DataCompanyDTO] = []
    cursor: str | None = None
    observed_cursors: set[str] = set()
    snapshot_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=6, headers=headers, transport=transport) as client:
            while True:
                response = await _request_page(
                    client,
                    url=f"{base_url}{COMPANIES_PATH}",
                    cursor=cursor,
                )
                if response.status_code == 409:
                    raise ProjectionError("Company API snapshot changed during pagination")
                response.raise_for_status()
                envelope = CompanyPageEnvelope.model_validate(response.json())
                page = envelope.result
                if snapshot_id is None:
                    snapshot_id = page.snapshot_id
                elif page.snapshot_id != snapshot_id:
                    raise ProjectionError("Company API snapshot changed during pagination")
                if page.next_cursor is not None and not page.items:
                    raise ProjectionError("Company API returned an empty nonterminal page")
                companies.extend(page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
                if cursor in observed_cursors:
                    raise ProjectionError("Company API repeated an opaque cursor")
                observed_cursors.add(cursor)
    except ProjectionError:
        raise
    except ValidationError:
        raise ProjectionError("Company API response violates its frozen DTO") from None
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.__class__.__name__
        if isinstance(exc, httpx.HTTPStatusError):
            detail = f"HTTP {exc.response.status_code}"
        raise ProjectionError(f"Company API request failed ({detail})") from None

    assert snapshot_id is not None
    facts = CompanyFacts(
        schema_version=COMPANY_PROJECTION_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        companies=tuple(companies),
    )
    _validate_complete_snapshot(facts.companies)
    return facts


def _company_summary(company: DataCompanyDTO) -> str:
    details = [f"企业：{company.name}", f"代码：{company.code}"]
    if company.legal_name is not None and company.legal_name != company.name:
        details.append(f"法定名称：{company.legal_name}")
    if company.strategic_positioning is not None:
        details.append(f"战略定位：{company.strategic_positioning}")
    if company.description is not None:
        details.append(company.description)
    return "。".join(details) + "。"


def build_company_node(company: DataCompanyDTO) -> EntityNode:
    """Build one canonical Company node without LLM identity resolution."""

    try:
        attributes = Company(
            data_object_id=company.id,
            code=company.code,
            name_en=company.name_en,
            legal_name=company.legal_name,
            aliases=company.aliases,
            registration_country_id=company.registration_country_id,
            operating_area=company.operating_area,
            headquarters_city=company.headquarters_city,
            founding_date=company.founding_date,
            ipo_date=company.ipo_date,
            legal_form=company.legal_form,
            ownership_type=company.ownership_type,
            strategic_positioning=company.strategic_positioning,
            description=company.description,
            status=company.status,
            source_record_fingerprint=company_source_fingerprint(company),
            updated_at=company.updated_at,
        ).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise ProjectionError(f"Company {company.id} violates ontology: {exc}") from None
    return EntityNode(
        uuid=node_uuid(company.id),
        name=company.name,
        group_id=GROUP_ID,
        labels=["Company"],
        created_at=company.created_at,
        summary=_company_summary(company),
        attributes=attributes,
    )


def build_formal_industry_edge(
    company: DataCompanyDTO,
    link: DataCompanyIndustryLinkDTO,
) -> EntityEdge:
    """Build one canonical formal edge while preserving its Data source-record time."""

    if link.company_id != company.id:
        raise ProjectionError(f"CompanyIndustryLink {link.id} has the wrong Company endpoint")
    try:
        fingerprint = _canonical_sha256(link.model_dump(mode="json"))
        attributes = CompanyBelongsToIndustry(
            data_object_id=link.id,
            source_company_id=company.id,
            target_data_object_id=link.industry_id,
            projection_fingerprint=fingerprint,
            source_record_created_at=link.created_at,
        ).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        raise ProjectionError(f"CompanyIndustryLink {link.id} violates ontology: {exc}") from None
    return EntityEdge(
        uuid=edge_uuid("CompanyBelongsToIndustry", company.id, link.industry_id),
        group_id=GROUP_ID,
        source_node_uuid=node_uuid(company.id),
        target_node_uuid=node_uuid(link.industry_id),
        created_at=link.created_at,
        name="CompanyBelongsToIndustry",
        fact=f"{company.name}的 Tidewise Data 正式行业归属指向 {link.industry_id}",
        attributes=attributes,
    )


def build_plan(facts: CompanyFacts) -> CompanyPlan:
    """Validate one complete snapshot before constructing authoritative projection data."""

    expected_industry_relations = ["CompanyBelongsToIndustry", "CompanyOperatesInIndustry"]
    if EDGE_TYPE_MAP.get(("Company", "Industry")) != expected_industry_relations:
        raise ProjectionError("ontology does not separate formal and inferred Company Industry relations")
    if EDGE_TYPE_MAP.get(("Company", "ChainNode")) != ["CompanyParticipatesInChainNode"]:
        raise ProjectionError("ontology does not permit inferred Company ChainNode participation")
    _validate_complete_snapshot(facts.companies)

    nodes: list[EntityNode] = []
    formal_edges: list[EntityEdge] = []
    formal_industry_ids: set[str] = set()
    for company in facts.companies:
        nodes.append(build_company_node(company))
        for link in company.industry_links:
            formal_edges.append(build_formal_industry_edge(company, link))
            formal_industry_ids.add(link.industry_id)
    return CompanyPlan(
        schema_version=facts.schema_version,
        snapshot_id=facts.snapshot_id,
        company_count=len(nodes),
        formal_industry_relation_count=len(formal_edges),
        nodes=tuple(nodes),
        formal_industry_edges=tuple(formal_edges),
        formal_industry_ids=frozenset(formal_industry_ids),
    )


def _target_pattern(label: TargetLabel) -> str:
    return INDUSTRY_ID_PATTERN if label == "Industry" else CHAIN_NODE_ID_PATTERN


async def preflight_canonical_targets(
    graphiti: Graphiti,
    target_labels: dict[str, TargetLabel],
) -> dict[str, CanonicalGraphTarget]:
    """Resolve only existing exact-label targets with deterministic canonical UUIDs."""

    if not target_labels:
        return {}
    problems: list[str] = []
    for target_id, label in target_labels.items():
        if label not in {"Industry", "ChainNode"}:
            problems.append(f"{target_id} has unsupported target label {label}")
            continue
        if re.fullmatch(_target_pattern(label), target_id) is None:
            problems.append(f"{target_id} is not a canonical {label} ID")
    if problems:
        raise ProjectionError(f"Company canonical target preflight failed: {problems[0]}")

    result = await graphiti.driver.execute_query(
        """
        MATCH (target:Entity {group_id: $group_id})
        WHERE target.data_object_id IN $target_ids
        RETURN target.data_object_id AS data_object_id, target.uuid AS uuid,
               target.name AS name, labels(target) AS labels
        ORDER BY data_object_id
        """,
        group_id=GROUP_ID,
        target_ids=sorted(target_labels),
    )
    records_by_id: dict[str, list[dict[str, object]]] = {}
    for raw_record in result.records:
        record = raw_record.data()
        if not isinstance(record, dict):
            problems.append("graph returned a non-object canonical target record")
            continue
        data_object_id = record.get("data_object_id")
        uuid = record.get("uuid")
        name = record.get("name")
        labels = record.get("labels")
        if (
            not isinstance(data_object_id, str)
            or not isinstance(uuid, str)
            or not isinstance(name, str)
            or not isinstance(labels, list)
            or any(not isinstance(label, str) for label in labels)
        ):
            problems.append("graph returned a malformed canonical target record")
            continue
        records_by_id.setdefault(data_object_id, []).append(record)

    targets: dict[str, CanonicalGraphTarget] = {}
    for target_id, label in target_labels.items():
        matches = records_by_id.get(target_id, [])
        expected_labels = {"Entity", label}
        if len(matches) != 1:
            problems.append(f"{target_id} resolves to {len(matches)} canonical nodes")
            continue
        match = matches[0]
        match_labels = match["labels"]
        if not isinstance(match_labels, list) or any(not isinstance(item, str) for item in match_labels):
            problems.append(f"{target_id} has malformed labels")
            continue
        if set(match_labels) != expected_labels:
            problems.append(f"{target_id} does not have exclusive labels {sorted(expected_labels)}")
            continue
        if match["uuid"] != node_uuid(target_id):
            problems.append(f"{target_id} does not use its deterministic Graphiti UUID")
            continue
        name = match["name"]
        if not isinstance(name, str) or not name.strip():
            problems.append(f"{target_id} has no canonical name")
            continue
        targets[target_id] = CanonicalGraphTarget(
            data_object_id=target_id,
            uuid=str(match["uuid"]),
            name=name,
            label=label,
        )
    unexpected_ids = set(records_by_id).difference(target_labels)
    if unexpected_ids:
        problems.append(f"query returned unexpected target {sorted(unexpected_ids)[0]}")
    if problems:
        raise ProjectionError(f"Company canonical target preflight failed: {problems[0]}")
    return targets


async def preflight_formal_industry_targets(
    graphiti: Graphiti,
    plan: CompanyPlan,
) -> dict[str, CanonicalGraphTarget]:
    """Fail closed if any formal CompanyIndustryLink target is absent or polluted."""

    return await preflight_canonical_targets(
        graphiti,
        {target_id: "Industry" for target_id in plan.formal_industry_ids},
    )


async def preflight_company_namespace(graphiti: Graphiti, plan: CompanyPlan) -> None:
    """Reject foreign or noncanonical nodes that could collide with this projection's identity scope."""

    expected_ids = {str(node.attributes["data_object_id"]): node.uuid for node in plan.nodes}
    expected_ids_by_uuid = {uuid: data_object_id for data_object_id, uuid in expected_ids.items()}
    expected_uuids = set(expected_ids_by_uuid)
    result = await graphiti.driver.execute_query(
        """
        MATCH (node)
        WHERE (node.group_id = $group_id AND (
                  node.projection_owner = $projection_owner
               OR node.data_object_id STARTS WITH 'COM'
              ))
           OR node.uuid IN $expected_node_uuids
        RETURN node.uuid AS uuid, node.data_object_id AS data_object_id,
               labels(node) AS labels, node.group_id AS group_id,
               node.projection_owner AS projection_owner
        ORDER BY data_object_id, uuid
        """,
        group_id=GROUP_ID,
        projection_owner=COMPANY_PROJECTION_OWNER,
        expected_node_uuids=sorted(expected_uuids),
    )
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_uuids: set[str] = set()
    for raw_record in result.records:
        record = raw_record.data()
        data_object_id = record.get("data_object_id")
        uuid = record.get("uuid")
        labels = record.get("labels")
        if (
            not isinstance(data_object_id, str)
            or re.fullmatch(COMPANY_ID_PATTERN, data_object_id) is None
            or not isinstance(uuid, str)
            or not isinstance(labels, list)
            or any(not isinstance(label, str) for label in labels)
        ):
            problems.append("Company identity namespace contains a malformed projected node")
            continue
        if data_object_id in seen_ids or uuid in seen_uuids:
            problems.append(f"Company identity namespace contains a duplicate: {data_object_id}")
            continue
        seen_ids.add(data_object_id)
        seen_uuids.add(uuid)
        if uuid != node_uuid(data_object_id):
            problems.append(f"Company {data_object_id} does not use its deterministic UUID")
        if record.get("group_id") != GROUP_ID:
            problems.append(f"Company {data_object_id} collides outside the projection group")
        if set(labels) != {"Entity", "Company"}:
            problems.append(f"Company {data_object_id} does not have exclusive canonical labels")
        if record.get("projection_owner") != COMPANY_PROJECTION_OWNER:
            problems.append(f"Company {data_object_id} is outside the owned projection namespace")
        expected_data_object_id = expected_ids_by_uuid.get(uuid)
        if uuid in expected_uuids and expected_data_object_id != data_object_id:
            problems.append(f"Company UUID collision does not match the Data identity: {uuid}")
    if problems:
        raise ProjectionError(f"Company identity namespace preflight failed: {problems[0]}")


async def preflight_company_relation_namespace(
    graphiti: Graphiti,
    expected_edges: Sequence[EntityEdge],
) -> None:
    """Reject wrong-direction, wrong-owner, or duplicate relationships before direct MERGE or verification."""

    expected_by_uuid = {edge.uuid: edge for edge in expected_edges}
    if len(expected_by_uuid) != len(expected_edges):
        raise ProjectionError("Company relation namespace preflight received duplicate expected UUIDs")
    result = await graphiti.driver.execute_query(
        """
        MATCH (source)-[edge]->(target)
        WHERE edge.projection_owner = $projection_owner
           OR (edge.group_id = $group_id AND edge.name IN $relation_names)
           OR edge.uuid IN $expected_edge_uuids
        RETURN edge.uuid AS uuid, type(edge) AS relationship_type,
               edge.name AS name, edge.group_id AS group_id,
               edge.projection_owner AS projection_owner,
               source.uuid AS source_uuid, source.data_object_id AS source_id,
               labels(source) AS source_labels, source.group_id AS source_group_id,
               source.projection_owner AS source_projection_owner,
               target.uuid AS target_uuid, target.data_object_id AS target_id,
               labels(target) AS target_labels, target.group_id AS target_group_id
        ORDER BY uuid, source_uuid, target_uuid
        """,
        group_id=GROUP_ID,
        projection_owner=COMPANY_PROJECTION_OWNER,
        relation_names=sorted(OWNED_COMPANY_EDGE_NAMES),
        expected_edge_uuids=sorted(expected_by_uuid),
    )
    problems: list[str] = []
    seen_uuids: set[str] = set()
    for raw_record in result.records:
        record = raw_record.data()
        uuid = record.get("uuid")
        name = record.get("name")
        source_uuid = record.get("source_uuid")
        source_id = record.get("source_id")
        source_labels = record.get("source_labels")
        target_uuid = record.get("target_uuid")
        target_id = record.get("target_id")
        target_labels = record.get("target_labels")
        if (
            not isinstance(uuid, str)
            or not isinstance(name, str)
            or not isinstance(source_uuid, str)
            or not isinstance(source_id, str)
            or not isinstance(source_labels, list)
            or any(not isinstance(label, str) for label in source_labels)
            or not isinstance(target_uuid, str)
            or not isinstance(target_id, str)
            or not isinstance(target_labels, list)
            or any(not isinstance(label, str) for label in target_labels)
        ):
            problems.append("Company relation namespace contains a malformed relationship")
            continue
        if uuid in seen_uuids:
            problems.append(f"Company relation namespace contains duplicate UUID {uuid}")
            continue
        seen_uuids.add(uuid)
        if name not in OWNED_COMPANY_EDGE_NAMES:
            problems.append(f"Company relation {uuid} has an unowned name")
            continue
        target_label: TargetLabel = "ChainNode" if name == "CompanyParticipatesInChainNode" else "Industry"
        if record.get("relationship_type") != "RELATES_TO":
            problems.append(f"Company relation {uuid} has the wrong Neo4j relationship type")
        if record.get("group_id") != GROUP_ID:
            problems.append(f"Company relation {uuid} collides outside the projection group")
        if record.get("projection_owner") != COMPANY_PROJECTION_OWNER:
            problems.append(f"Company relation {uuid} is outside the owned projection namespace")
        if (
            re.fullmatch(COMPANY_ID_PATTERN, source_id) is None
            or source_uuid != node_uuid(source_id)
            or set(source_labels) != {"Entity", "Company"}
            or record.get("source_group_id") != GROUP_ID
            or record.get("source_projection_owner") != COMPANY_PROJECTION_OWNER
        ):
            problems.append(f"Company relation {uuid} has a noncanonical source")
        if (
            re.fullmatch(_target_pattern(target_label), target_id) is None
            or target_uuid != node_uuid(target_id)
            or set(target_labels) != {"Entity", target_label}
            or record.get("target_group_id") != GROUP_ID
        ):
            problems.append(f"Company relation {uuid} has a noncanonical target")
        if uuid != edge_uuid(name, source_id, target_id):
            problems.append(f"Company relation {uuid} does not use its deterministic endpoint UUID")
        expected = expected_by_uuid.get(uuid)
        if expected is not None and (
            expected.name != name
            or expected.source_node_uuid != source_uuid
            or expected.target_node_uuid != target_uuid
        ):
            problems.append(f"Company relation {uuid} collides with different endpoints or type")
    if problems:
        raise ProjectionError(f"Company relation namespace preflight failed: {problems[0]}")


def _inferred_attributes(
    decision: CompanyInferenceDecision,
    target_id: str,
    selection,
) -> dict[str, object]:
    source_industry_ids = selection.source_industry_ids
    if target_id.startswith("IND") and not source_industry_ids:
        source_industry_ids = [target_id]
    base = {
        "derivation_type": "MODEL_INFERRED",
        "projection_owner": COMPANY_PROJECTION_OWNER,
        "decision_id": decision.decision_id,
        "source_company_id": decision.company_id,
        "target_data_object_id": target_id,
        "confidence": selection.confidence.value,
        "rationale": selection.rationale,
        "source_company_fingerprint": decision.source_company_fingerprint,
        "target_catalog_fingerprint": decision.target_catalog_fingerprint,
        "model_id": decision.model_id,
        "prompt_contract_version": decision.prompt_contract_version,
        "ontology_version": decision.ontology_version,
        "policy_version": decision.policy_version,
        "supporting_company_fields": selection.supporting_company_fields,
        "source_industry_ids": source_industry_ids,
        "industry_chain_ids": selection.industry_chain_ids,
        "decided_at": decision.decided_at,
    }
    return {**base, "projection_fingerprint": _canonical_sha256(base)}


def build_inferred_edges(
    facts: CompanyFacts,
    decisions: Sequence[CompanyInferenceDecision],
    canonical_targets: dict[str, CanonicalGraphTarget],
) -> tuple[EntityEdge, ...]:
    """Build direct EntityEdges from frozen decisions; never create or resolve target nodes."""

    companies_by_id = {company.id: company for company in facts.companies}
    decisions_by_id = {decision.company_id: decision for decision in decisions}
    if len(decisions_by_id) != len(decisions):
        raise ValueError("duplicate Company inference decision")
    if set(decisions_by_id) != set(companies_by_id):
        raise ValueError("Company inference decisions do not exactly cover the Data snapshot")
    edges: list[EntityEdge] = []
    for input_index, company in enumerate(facts.companies):
        decision = decisions_by_id[company.id]
        if decision.snapshot_id != facts.snapshot_id:
            raise ValueError(f"Company decision snapshot mismatch: {company.id}")
        expected_subject = company_subject(company, input_index)
        if decision.input_index != input_index or decision.source_company_fingerprint != expected_subject.fingerprint():
            raise ValueError(f"Company input mismatch for frozen decision: {company.id}")
        stages = (
            (decision.industry.accepted_targets, "Industry", "CompanyOperatesInIndustry"),
            (decision.chain_node.accepted_targets, "ChainNode", "CompanyParticipatesInChainNode"),
        )
        for selections, expected_label, relation_name in stages:
            for selection in selections:
                if selection.confidence not in {Confidence.MEDIUM, Confidence.HIGH}:
                    raise ValueError("LOW confidence cannot produce a Company relation")
                target = canonical_targets.get(selection.target_id)
                if target is None:
                    raise ValueError(f"target {selection.target_id} is not in the canonical preflight catalog")
                if target.label != expected_label:
                    raise ValueError(f"target {selection.target_id} has the wrong canonical label")
                raw_attributes = _inferred_attributes(decision, selection.target_id, selection)
                link_type = (
                    CompanyOperatesInIndustry
                    if relation_name == "CompanyOperatesInIndustry"
                    else CompanyParticipatesInChainNode
                )
                attributes = link_type.model_validate(raw_attributes).model_dump(mode="json")
                target_kind = "行业" if expected_label == "Industry" else "产业链节点"
                edges.append(
                    EntityEdge(
                        uuid=edge_uuid(relation_name, company.id, selection.target_id),
                        group_id=GROUP_ID,
                        source_node_uuid=node_uuid(company.id),
                        target_node_uuid=target.uuid,
                        created_at=decision.decided_at,
                        name=relation_name,
                        fact=f"{company.name}被模型判断为直接参与{target_kind}{target.name}：{selection.rationale}",
                        attributes=attributes,
                    )
                )
    if len({edge.uuid for edge in edges}) != len(edges):
        raise ValueError("duplicate inferred Company relation endpoints")
    return tuple(edges)


def _record_labels(record: Mapping[str, object], key: str) -> set[str]:
    value = record.get(key)
    if not isinstance(value, list) or any(not isinstance(label, str) for label in value):
        raise ProjectionError(f"Company graph inspection returned malformed {key}")
    return set(value)


def diff_company_projection(
    plan: CompanyPlan,
    inferred_edges: Sequence[EntityEdge],
    state: Mapping[str, object],
    *,
    embedding_dimension: int,
) -> tuple[list[EntityNode], list[EntityEdge]]:
    """Select only semantic changes or missing embeddings for direct upsert."""

    nodes = state.get("nodes")
    edges = state.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ProjectionError("Company graph inspection returned an invalid state")
    existing_nodes: dict[str, dict[str, object]] = {}
    for record in nodes:
        if not isinstance(record, dict) or not isinstance(record.get("uuid"), str):
            raise ProjectionError("Company graph inspection returned an invalid node")
        if record["uuid"] in existing_nodes:
            raise ProjectionError(f"duplicate Company graph UUID: {record['uuid']}")
        existing_nodes[record["uuid"]] = record
    expected_edges = [*plan.formal_industry_edges, *inferred_edges]
    existing_edges: dict[str, dict[str, object]] = {}
    for record in edges:
        if not isinstance(record, dict) or not isinstance(record.get("uuid"), str):
            raise ProjectionError("Company graph inspection returned an invalid edge")
        if record["uuid"] in existing_edges:
            raise ProjectionError(f"duplicate Company relation UUID: {record['uuid']}")
        existing_edges[record["uuid"]] = record
    changed_nodes = [
        node
        for node in plan.nodes
        if (record := existing_nodes.get(node.uuid)) is None
        or record.get("data_object_id") != node.attributes["data_object_id"]
        or _record_labels(record, "labels") != {"Entity", "Company"}
        or record.get("name") != node.name
        or record.get("summary") != node.summary
        or record.get("source_record_fingerprint") != node.attributes["source_record_fingerprint"]
        or record.get("embedding_dimension") != embedding_dimension
    ]
    changed_edges = [
        edge
        for edge in expected_edges
        if (record := existing_edges.get(edge.uuid)) is None
        or record.get("name") != edge.name
        or record.get("source_id") != edge.attributes["source_company_id"]
        or record.get("target_id") != edge.attributes["target_data_object_id"]
        or record.get("fact") != edge.fact
        or record.get("projection_fingerprint") != edge.attributes["projection_fingerprint"]
        or record.get("embedding_dimension") != embedding_dimension
    ]
    return changed_nodes, changed_edges


async def inspect_company_projection_state(graphiti: Graphiti) -> dict[str, object]:
    """Inspect only the Company projection scope and its owned relationship names."""

    node_result = await graphiti.driver.execute_query(
        """
        MATCH (company:Entity:Company {group_id: $group_id, projection_owner: $projection_owner})
        WHERE company.data_object_id STARTS WITH 'COM'
        RETURN company.uuid AS uuid, company.data_object_id AS data_object_id,
               labels(company) AS labels, company.name AS name, company.summary AS summary,
               company.source_record_fingerprint AS source_record_fingerprint,
               properties(company) AS properties,
               size(company.name_embedding) AS embedding_dimension
        ORDER BY data_object_id, uuid
        """,
        group_id=GROUP_ID,
        projection_owner=COMPANY_PROJECTION_OWNER,
    )
    edge_result = await graphiti.driver.execute_query(
        """
        MATCH (source:Entity:Company {group_id: $group_id, projection_owner: $projection_owner})
              -[edge:RELATES_TO]->(target:Entity)
        WHERE edge.name IN $relation_names AND edge.projection_owner = $projection_owner
        RETURN edge.uuid AS uuid, edge.name AS name, edge.fact AS fact,
               edge.projection_fingerprint AS projection_fingerprint,
               properties(edge) AS properties,
               source.data_object_id AS source_id, labels(source) AS source_labels,
               target.data_object_id AS target_id, target.uuid AS target_uuid,
               labels(target) AS target_labels,
               size(edge.fact_embedding) AS embedding_dimension
        ORDER BY name, source_id, target_id, uuid
        """,
        group_id=GROUP_ID,
        projection_owner=COMPANY_PROJECTION_OWNER,
        relation_names=sorted(OWNED_COMPANY_EDGE_NAMES),
    )
    return {
        "nodes": [record.data() for record in node_result.records],
        "edges": [record.data() for record in edge_result.records],
    }


def verify_company_projection(
    plan: CompanyPlan,
    inferred_edges: Sequence[EntityEdge],
    state: Mapping[str, object],
    *,
    embedding_dimension: int,
) -> dict[str, object]:
    """Prove exact Company identity and relation parity after a complete run."""

    nodes = state.get("nodes")
    edges = state.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ProjectionError("Company graph inspection returned an invalid state")
    expected_nodes = {node.uuid: node for node in plan.nodes}
    expected_edges = {edge.uuid: edge for edge in [*plan.formal_industry_edges, *inferred_edges]}
    if len(expected_edges) != len(plan.formal_industry_edges) + len(inferred_edges):
        raise ProjectionError("Company projection contains duplicate relationship UUIDs")
    actual_node_uuids = [record.get("uuid") for record in nodes if isinstance(record, dict)]
    actual_edge_uuids = [record.get("uuid") for record in edges if isinstance(record, dict)]
    problems: list[str] = []
    if set(actual_node_uuids) != set(expected_nodes) or len(actual_node_uuids) != len(expected_nodes):
        problems.append("Company ID set differs from the Data snapshot")
    if set(actual_edge_uuids) != set(expected_edges) or len(actual_edge_uuids) != len(expected_edges):
        problems.append("Company relation set differs from frozen facts and decisions")
    for record in nodes:
        if not isinstance(record, dict):
            problems.append("Company graph contains an invalid node record")
            continue
        expected_node = expected_nodes.get(str(record.get("uuid")))
        if expected_node is None:
            continue
        if record.get("data_object_id") != expected_node.attributes["data_object_id"]:
            problems.append("Company node has a wrong Data identity")
        if _record_labels(record, "labels") != {"Entity", "Company"}:
            problems.append("Company labels are not exclusive")
        if record.get("name") != expected_node.name or record.get("summary") != expected_node.summary:
            problems.append("Company display facts differ from the Data snapshot")
        if record.get("source_record_fingerprint") != expected_node.attributes["source_record_fingerprint"]:
            problems.append("Company source fingerprint differs from the Data snapshot")
        properties = record.get("properties")
        if not isinstance(properties, dict) or any(
            properties.get(key) != value for key, value in expected_node.attributes.items()
        ):
            problems.append("Company node attributes differ from the Data snapshot")
        if record.get("embedding_dimension") != embedding_dimension:
            problems.append("Company embedding is missing or has the wrong dimension")
    for record in edges:
        if not isinstance(record, dict):
            problems.append("Company graph contains an invalid edge record")
            continue
        expected_edge = expected_edges.get(str(record.get("uuid")))
        if expected_edge is None:
            continue
        expected_target_label = "ChainNode" if expected_edge.name == "CompanyParticipatesInChainNode" else "Industry"
        if record.get("name") != expected_edge.name:
            problems.append("Company relation type differs from its frozen source")
        if _record_labels(record, "source_labels") != {"Entity", "Company"}:
            problems.append("Company relation has a wrongly typed source")
        if _record_labels(record, "target_labels") != {"Entity", expected_target_label}:
            problems.append("Company relation has a wrongly typed target")
        if record.get("source_id") != expected_edge.attributes["source_company_id"]:
            problems.append("Company relation has a wrong source identity")
        if record.get("target_id") != expected_edge.attributes["target_data_object_id"]:
            problems.append("Company relation has a wrong target identity")
        if record.get("target_uuid") != expected_edge.target_node_uuid:
            problems.append("Company relation target does not use its deterministic UUID")
        if record.get("fact") != expected_edge.fact:
            problems.append("Company relation fact differs from its frozen source")
        if record.get("projection_fingerprint") != expected_edge.attributes["projection_fingerprint"]:
            problems.append("Company relation fingerprint differs from its frozen source")
        properties = record.get("properties")
        if not isinstance(properties, dict) or any(
            properties.get(key) != value for key, value in expected_edge.attributes.items()
        ):
            problems.append("Company relation attributes differ from its frozen source")
        if record.get("embedding_dimension") != embedding_dimension:
            problems.append("Company relation embedding is missing or has the wrong dimension")
    if problems:
        raise ProjectionError("; ".join(dict.fromkeys(problems)))
    return {
        **plan.summary(),
        "inferred_industry_relations": sum(edge.name == "CompanyOperatesInIndustry" for edge in inferred_edges),
        "inferred_chain_node_relations": sum(edge.name == "CompanyParticipatesInChainNode" for edge in inferred_edges),
        "node_total": len(nodes),
        "relation_total": len(edges),
        "verified": True,
    }


async def execute_company_projection(
    graphiti: Graphiti,
    facts: CompanyFacts,
    plan: CompanyPlan,
    decisions: Sequence[CompanyInferenceDecision],
    manifest: ProjectionRunManifest,
    *,
    embedding_dimension: int,
    replace: bool,
    progress=None,
) -> dict[str, object]:
    """Directly upsert changed EntityNodes/EntityEdges and sweep only after complete decisions."""

    if plan.snapshot_id != facts.snapshot_id or plan.company_count != len(facts.companies):
        raise ProjectionError("Company plan does not match the complete Data snapshot")
    if manifest.snapshot_id != facts.snapshot_id or manifest.company_ids != [item.id for item in facts.companies]:
        raise ProjectionError("Company run manifest does not match the complete Data snapshot")
    await preflight_company_namespace(graphiti, plan)
    await _validate_current_target_catalog(graphiti, manifest, decisions)
    target_labels: dict[str, TargetLabel] = {target_id: "Industry" for target_id in plan.formal_industry_ids}
    for decision in decisions:
        for target in decision.industry.accepted_targets:
            target_labels[target.target_id] = "Industry"
        for target in decision.chain_node.accepted_targets:
            target_labels[target.target_id] = "ChainNode"
    canonical_targets = await preflight_canonical_targets(graphiti, target_labels)
    inferred_edges = build_inferred_edges(facts, decisions, canonical_targets)
    expected_edges = [*plan.formal_industry_edges, *inferred_edges]
    await preflight_company_relation_namespace(graphiti, expected_edges)
    before = await inspect_company_projection_state(graphiti)
    changed_nodes, changed_edges = diff_company_projection(
        plan,
        inferred_edges,
        before,
        embedding_dimension=embedding_dimension,
    )
    await _write_changed_company_facts(
        graphiti,
        changed_nodes,
        changed_edges,
        progress=progress,
    )
    removed = {"nodes": 0, "relationships": 0}
    if replace:
        await preflight_company_namespace(graphiti, plan)
        await _validate_current_target_catalog(graphiti, manifest, decisions)
        await preflight_company_relation_namespace(graphiti, expected_edges)
        expected_node_uuids = {node.uuid for node in plan.nodes}
        expected_edge_uuids = {edge.uuid for edge in expected_edges}
        before_nodes = before["nodes"]
        before_edges = before["edges"]
        assert isinstance(before_nodes, list) and isinstance(before_edges, list)
        stale_node_uuids = sorted(
            str(record["uuid"]) for record in before_nodes if record.get("uuid") not in expected_node_uuids
        )
        stale_edge_uuids = sorted(
            str(record["uuid"]) for record in before_edges if record.get("uuid") not in expected_edge_uuids
        )
        removed = {"nodes": len(stale_node_uuids), "relationships": len(stale_edge_uuids)}
        if stale_edge_uuids:
            await graphiti.driver.execute_query(
                """
                MATCH ()-[edge]->()
                WHERE edge.group_id = $group_id
                  AND edge.projection_owner = $projection_owner
                  AND edge.name IN $relation_names
                  AND edge.uuid IN $stale_edge_uuids
                DELETE edge
                """,
                group_id=GROUP_ID,
                projection_owner=COMPANY_PROJECTION_OWNER,
                relation_names=sorted(OWNED_COMPANY_EDGE_NAMES),
                stale_edge_uuids=stale_edge_uuids,
            )
        if stale_node_uuids:
            await graphiti.driver.execute_query(
                """
                MATCH (company:Entity:Company {group_id: $group_id, projection_owner: $projection_owner})
                WHERE company.data_object_id STARTS WITH 'COM'
                  AND company.uuid IN $stale_node_uuids
                DELETE company
                """,
                group_id=GROUP_ID,
                projection_owner=COMPANY_PROJECTION_OWNER,
                stale_node_uuids=stale_node_uuids,
            )
    await preflight_company_namespace(graphiti, plan)
    await preflight_company_relation_namespace(graphiti, expected_edges)
    await _validate_current_target_catalog(graphiti, manifest, decisions)
    after = await inspect_company_projection_state(graphiti)
    result: dict[str, object] = {
        **plan.summary(),
        "nodes_written": len(changed_nodes),
        "relations_written": len(changed_edges),
        "removed_after_complete_write": removed,
        "replaced": replace,
        "write_mode": "direct-entity-node-edge-bulk-no-episode",
    }
    if replace:
        result.update(
            verify_company_projection(
                plan,
                inferred_edges,
                after,
                embedding_dimension=embedding_dimension,
            )
        )
    else:
        result.update({"verified": False, "verification_scope": "upsert-only"})
    return result


async def _validate_current_target_catalog(
    graphiti: Graphiti,
    manifest: ProjectionRunManifest,
    decisions: Sequence[CompanyInferenceDecision],
) -> TargetCatalog:
    catalog = await load_company_target_catalog(graphiti)
    catalog_fingerprint = catalog.fingerprint()
    if catalog_fingerprint != manifest.target_catalog_fingerprint:
        raise ProjectionError("canonical Industry or supply-chain target catalog changed during Company projection")
    try:
        for decision in decisions:
            validate_decision_candidate_scope(
                decision,
                catalog,
                manifest,
                catalog_fingerprint=catalog_fingerprint,
            )
    except ValueError as exc:
        raise ProjectionError(f"frozen Company candidate audit is invalid: {exc}") from None
    return catalog


async def _write_changed_company_facts(
    graphiti: Graphiti,
    nodes: Sequence[EntityNode],
    edges: Sequence[EntityEdge],
    *,
    progress: Callable[[int, int], None] | None,
) -> None:
    """Bound embedding residency and Neo4j transactions to one explicit chunk."""

    total = len(nodes) + len(edges)
    completed = 0

    async def write_chunk(
        node_chunk: list[EntityNode],
        edge_chunk: list[EntityEdge],
    ) -> None:
        nonlocal completed

        offset = completed

        def chunk_progress(done: int, _chunk_total: int) -> None:
            if progress is not None:
                progress(offset + done, total)

        try:
            await write_projection(
                graphiti,
                nodes=node_chunk,
                edges=edge_chunk,
                owned_node_labels=frozenset({"Company"}),
                owned_edge_names=OWNED_COMPANY_EDGE_NAMES,
                replace=False,
                progress=chunk_progress if progress is not None else None,
            )
        finally:
            for node in node_chunk:
                node.name_embedding = None
            for edge in edge_chunk:
                edge.fact_embedding = None
        completed += len(node_chunk) + len(edge_chunk)

    for start in range(0, len(nodes), COMPANY_WRITE_BATCH_SIZE):
        await write_chunk(list(nodes[start : start + COMPANY_WRITE_BATCH_SIZE]), [])
    for start in range(0, len(edges), COMPANY_WRITE_BATCH_SIZE):
        await write_chunk([], list(edges[start : start + COMPANY_WRITE_BATCH_SIZE]))
