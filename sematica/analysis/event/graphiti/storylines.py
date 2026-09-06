"""Read authoritative matching profiles without semantic retrieval or graph mutation."""

from typing import Any

from graphiti_core.search.search_config import NodeReranker, NodeSearchConfig, NodeSearchMethod, SearchConfig
from graphiti_core.search.search_filters import SearchFilters

from sematica.projection.runtime import GRAPHITI_GROUP_ID

PROFILE_FIELDS = {
    "GeopoliticRivalry": (
        "aliases",
        "category",
        "core_proposition",
        "core_actors",
        "domain_code",
        "domain_name",
        "domain_description",
        "tactics",
    ),
    "MacroEconomic": ("aliases", "core_proposition", "domain_code", "domain_name", "domain_description", "tactics"),
    "IndustryChain": (
        "aliases",
        "scope",
        "target_output",
        "end_use",
        "geography",
        "technology_route_qualifier",
        "observable_variables",
    ),
    "ChainNode": ("aliases", "definition"),
    "Company": ("aliases", "description"),
}


class GraphitiStorylineCatalog:
    def __init__(self, graphiti: Any):
        self._graphiti = graphiti
        self._driver = graphiti.driver

    async def companies(self, terms: list[str]) -> list[dict[str, Any]]:
        """Exact names/aliases plus bounded lexical/vector recall; never an identity decision."""
        exact = await self.profiles(["Company"], terms=terms)
        by_id = {p["uuid"]: p for p in exact}
        query = " ".join(sorted({term.strip() for term in terms if term.strip()}))
        if query:
            result = await self._graphiti.search_(
                query,
                config=SearchConfig(
                    node_config=NodeSearchConfig(
                        search_methods=[NodeSearchMethod.bm25, NodeSearchMethod.cosine_similarity],
                        reranker=NodeReranker.rrf,
                    ),
                    limit=8,
                ),
                group_ids=[GRAPHITI_GROUP_ID],
                search_filter=SearchFilters(node_labels=["Company"]),
            )
            for node in result.nodes:
                if node.group_id != GRAPHITI_GROUP_ID or "Company" not in node.labels:
                    continue
                attrs = node.attributes or {}
                if not attrs.get("data_object_id"):
                    continue
                by_id.setdefault(
                    node.uuid,
                    {
                        "uuid": node.uuid,
                        "business_id": attrs["data_object_id"],
                        "name": node.name,
                        "entity_type": "Company",
                        "profile": {key: attrs[key] for key in PROFILE_FIELDS["Company"] if attrs.get(key) is not None},
                    },
                )
        return [by_id[key] for key in sorted(by_id)]

    async def profiles(
        self, labels: list[str], *, terms: list[str] | None = None, chain_uuids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        if not labels or not set(labels) <= PROFILE_FIELDS.keys():
            raise ValueError("unsupported authoritative catalog label")
        records, _, _ = await self._driver.execute_query(
            """
            /* event_storyline_complete_catalog */
            MATCH (n:Entity {group_id: $group_id})
            WHERE any(label IN $labels WHERE label IN labels(n))
              AND coalesce(n.data_object_id, '') <> ''
              AND ($terms IS NULL OR toLower(n.name) IN $terms
                   OR any(alias IN coalesce(n.aliases, []) WHERE toLower(alias) IN $terms))
              AND ($chain_uuids IS NULL OR EXISTS {
                  MATCH (n)-[r:RELATES_TO]->(chain:IndustryChain {group_id: $group_id})
                  WHERE r.name = 'ChainNodeBelongsToIndustryChain'
                    AND r.group_id = $group_id AND chain.uuid IN $chain_uuids
              })
            RETURN n { .uuid, .data_object_id, .name, .aliases, .category,
                       .core_proposition, .core_actors, .domain_code, .domain_name,
                       .domain_description, .tactics, .scope, .target_output, .end_use,
                       .geography, .technology_route_qualifier, .observable_variables,
                       .definition, .description } AS properties, labels(n) AS labels
            ORDER BY n.uuid
            """,
            group_id=GRAPHITI_GROUP_ID,
            labels=labels,
            terms=None if terms is None else sorted({term.strip().lower() for term in terms if term.strip()}),
            chain_uuids=chain_uuids,
            routing_="r",
        )
        result = []
        seen: set[str] = set()
        for row in records:
            kinds = set(row["labels"]) & PROFILE_FIELDS.keys()
            if len(kinds) != 1:
                raise ValueError("ambiguous authoritative entity labels")
            kind = next(iter(kinds))
            props = row["properties"]
            if props["uuid"] in seen:
                raise ValueError("duplicate authoritative entity UUID")
            seen.add(props["uuid"])
            result.append(
                {
                    "uuid": props["uuid"],
                    "business_id": props["data_object_id"],
                    "name": props["name"],
                    "entity_type": kind,
                    "profile": {key: props[key] for key in PROFILE_FIELDS[kind] if props.get(key) is not None},
                }
            )
        return result

    async def variables(self) -> list[dict[str, Any]]:
        rows, _, _ = await self._driver.execute_query(
            """
            /* event_storyline_complete_variables */
            MATCH (v:Variable {group_id: $group_id, variable_role: 'FUNDAMENTAL'})
            RETURN v.uuid AS uuid, v.name AS name, v.variable_id AS variable_id,
                   v.variable_group AS variable_group, v.allowed_anchor_types AS allowed_anchor_types,
                   v.definition AS definition
            ORDER BY v.variable_id, v.uuid
            """,
            group_id=GRAPHITI_GROUP_ID,
            routing_="r",
        )
        return [dict(row) for row in rows]
