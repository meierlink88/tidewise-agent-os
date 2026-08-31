"""Project reviewed direct Signals through Graphiti's public Fact triple interface."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode, EpisodicNode

from sematica.analysis.event.contracts import (
    AnchorCandidate,
    EventAnalysisInput,
    EventClassification,
    SignalFactAttributes,
    SignalProposal,
    VariableCandidate,
)
from sematica.analysis.event.errors import PermanentEventAnalysisFailure
from sematica.ingestion.episcode.event.provenance import (
    EVENT_SOURCE_DESCRIPTION,
    event_episode_uuid,
    formal_event_id_from_content,
)
from sematica.projection.runtime import GRAPHITI_GROUP_ID

SIGNAL_RELATION_NAME = "SIGNAL_ON"
METHODOLOGY_VERSION = "event-analysis/v1"


def _signal_uuid(event_id: str, variable_id: str, anchor_uuid: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"urn:tidewise:direct-signal:{event_id}:{variable_id}:{anchor_uuid}",
        )
    )


class GraphitiSignalFactProjector:
    """Write one Signal Fact only after both exact endpoint identities exist."""

    def __init__(self, graphiti: Graphiti) -> None:
        self._graphiti = graphiti

    async def project(
        self,
        event: EventAnalysisInput,
        classification: EventClassification,
        variable: VariableCandidate,
        anchor: AnchorCandidate,
        proposal: SignalProposal,
    ) -> str:
        try:
            variable_node = await EntityNode.get_by_uuid(self._graphiti.driver, variable.uuid)
            anchor_node = await EntityNode.get_by_uuid(self._graphiti.driver, anchor.uuid)
            episode = await EpisodicNode.get_by_uuid(self._graphiti.driver, event.episode_uuid)
        except NodeNotFoundError as exc:
            raise PermanentEventAnalysisFailure("Signal endpoint requires an existing graph identity") from exc

        self._validate_variable(variable_node, variable)
        self._validate_anchor(anchor_node, anchor)
        self._validate_episode(episode, event)
        await self._require_formal_event_episode(event)

        attributes = SignalFactAttributes(
            source_event_ids=[event.event.id],
            event_class=classification.event_class,
            variable_id=variable.variable_id,
            anchor_type=anchor.entity_type,
            anchor_business_id=anchor.business_id,
            direction=proposal.direction,
            magnitude=proposal.magnitude,
            derivation_type=proposal.derivation_type,
            assertion_modality=proposal.assertion_modality,
            impact_onset_earliest=proposal.impact_onset_earliest,
            impact_onset_latest=proposal.impact_onset_latest,
            impact_peak_earliest=proposal.impact_peak_earliest,
            impact_peak_latest=proposal.impact_peak_latest,
            expected_end_earliest=proposal.expected_end_earliest,
            expected_end_latest=proposal.expected_end_latest,
            horizon_tags=list(proposal.horizon_tags),
            mechanism=proposal.mechanism,
            duration_basis=proposal.duration_basis,
            assumptions=proposal.assumptions,
            invalidation_conditions=proposal.invalidation_conditions,
            provenance_confidence=proposal.provenance_confidence,
            mechanism_confidence=proposal.mechanism_confidence,
            temporal_confidence=proposal.temporal_confidence,
            methodology_version=METHODOLOGY_VERSION,
        ).model_dump(mode="json", exclude_none=True)
        edge = EntityEdge(
            uuid=_signal_uuid(event.event.id, variable.variable_id, anchor.uuid),
            group_id=GRAPHITI_GROUP_ID,
            source_node_uuid=variable.uuid,
            target_node_uuid=anchor.uuid,
            created_at=datetime.now(UTC),
            name=SIGNAL_RELATION_NAME,
            fact=proposal.fact,
            episodes=[event.episode_uuid],
            valid_at=proposal.valid_at,
            invalid_at=proposal.invalid_at,
            reference_time=event.reference_time,
            attributes=attributes,
        )

        result = await self._graphiti.add_triplet(variable_node, edge, anchor_node)
        if not result.edges:
            raise PermanentEventAnalysisFailure("Graphiti returned no resolved Signal Fact")
        resolved = result.edges[0]
        if resolved.source_node_uuid != variable.uuid or resolved.target_node_uuid != anchor.uuid:
            raise PermanentEventAnalysisFailure("Graphiti resolved Signal Fact to unexpected endpoints")
        if resolved.group_id != GRAPHITI_GROUP_ID:
            raise PermanentEventAnalysisFailure("Graphiti resolved Signal Fact to an unexpected group")

        existing_source_ids = resolved.attributes.get("source_event_ids", [])
        if not isinstance(existing_source_ids, list):
            raise PermanentEventAnalysisFailure("resolved Signal Fact has invalid source_event_ids")
        resolved.name = SIGNAL_RELATION_NAME
        resolved.attributes.update(attributes)
        resolved.attributes["source_event_ids"] = sorted(
            {str(value) for value in existing_source_ids} | {event.event.id}
        )
        resolved.episodes = await self._existing_event_episode_ids(resolved.episodes, current=episode)
        await resolved.save(self._graphiti.driver)

        await self._link_event_episode(
            episode_uuid=episode.uuid,
            event_id=event.event.id,
            fact_uuid=resolved.uuid,
        )
        return resolved.uuid

    async def _link_event_episode(self, *, episode_uuid: str, event_id: str, fact_uuid: str) -> None:
        """Append Fact provenance without serializing the partial EpisodicNode model.

        Graphiti's public EpisodicNode does not model Tidewise's episode_kind and
        domain_object_id extensions. Saving that partial model would therefore
        remove those properties from Neo4j. This targeted update changes only the
        native entity_edges field and fails closed if the formal Event identity is
        missing.
        """

        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* signal_fact_link_event_episode */
            MATCH (episode:Episodic {
                uuid: $episode_uuid,
                group_id: $group_id,
                episode_kind: 'EVENT',
                domain_object_id: $event_id
            })
            WHERE episode.source_description = $source_description
            SET episode.entity_edges = CASE
                WHEN $fact_uuid IN coalesce(episode.entity_edges, [])
                    THEN coalesce(episode.entity_edges, [])
                ELSE coalesce(episode.entity_edges, []) + $fact_uuid
            END
            RETURN episode.uuid AS uuid,
                   episode.episode_kind AS episode_kind,
                   episode.domain_object_id AS domain_object_id,
                   episode.entity_edges AS entity_edges
            """,
            episode_uuid=episode_uuid,
            event_id=event_id,
            fact_uuid=fact_uuid,
            group_id=GRAPHITI_GROUP_ID,
            source_description=EVENT_SOURCE_DESCRIPTION,
        )
        if len(records) != 1:
            raise PermanentEventAnalysisFailure("formal Event Episode metadata is missing during Signal projection")
        row = records[0]
        if (
            str(row["uuid"]) != episode_uuid
            or row["episode_kind"] != "EVENT"
            or row["domain_object_id"] != event_id
            or fact_uuid not in row["entity_edges"]
        ):
            raise PermanentEventAnalysisFailure("Signal provenance was not linked to the formal Event Episode")

    async def _require_formal_event_episode(self, event: EventAnalysisInput) -> None:
        """Validate Tidewise's extension fields before native add_triplet can write."""

        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* signal_fact_require_formal_event_episode */
            MATCH (episode:Episodic {
                uuid: $episode_uuid,
                group_id: $group_id,
                episode_kind: 'EVENT',
                domain_object_id: $event_id
            })
            WHERE episode.source_description = $source_description
            RETURN episode.uuid AS uuid
            """,
            episode_uuid=event.episode_uuid,
            event_id=event.event.id,
            group_id=GRAPHITI_GROUP_ID,
            source_description=EVENT_SOURCE_DESCRIPTION,
        )
        if len(records) != 1 or str(records[0]["uuid"]) != event.episode_uuid:
            raise PermanentEventAnalysisFailure("Signal provenance requires formal Event Episode metadata")

    async def _existing_event_episode_ids(self, episode_uuids: list[str], *, current: EpisodicNode) -> list[str]:
        candidates = sorted(set(episode_uuids) | {current.uuid})
        records, _, _ = await self._graphiti.driver.execute_query(
            """
            /* signal_fact_existing_event_provenance */
            UNWIND $episode_uuids AS episode_uuid
            MATCH (episode:Episodic {uuid: episode_uuid, group_id: $group_id})
            WHERE episode.source_description = $source_description
            RETURN episode.uuid AS uuid, episode.content AS content
            """,
            episode_uuids=candidates,
            group_id=GRAPHITI_GROUP_ID,
            source_description=EVENT_SOURCE_DESCRIPTION,
            routing_="r",
        )
        resolved = sorted(
            {str(row["uuid"]) for row in records if formal_event_id_from_content(str(row["content"])) is not None}
        )
        if current.uuid not in resolved:
            raise PermanentEventAnalysisFailure("current Event Episode lost its provenance identity")
        return resolved

    @staticmethod
    def _validate_variable(node: EntityNode, expected: VariableCandidate) -> None:
        if (
            node.uuid != expected.uuid
            or "Variable" not in node.labels
            or node.attributes.get("variable_id") != expected.variable_id
            or node.attributes.get("variable_role") != "FUNDAMENTAL"
        ):
            raise PermanentEventAnalysisFailure("Variable endpoint is not an eligible existing identity")

    @staticmethod
    def _validate_anchor(node: EntityNode, expected: AnchorCandidate) -> None:
        if expected.entity_type.value == "IndustryChain":
            raise PermanentEventAnalysisFailure("IndustryChain cannot own a direct Signal Fact")
        if node.uuid != expected.uuid or expected.entity_type.value not in node.labels:
            raise PermanentEventAnalysisFailure("Anchor endpoint is not an eligible existing identity")
        stable_id = (
            node.attributes.get("data_object_id")
            or node.attributes.get("demo_catalog_key")
            or node.attributes.get("policy_key")
        )
        if stable_id != expected.business_id:
            raise PermanentEventAnalysisFailure("Anchor endpoint has no matching stable business identity")

    @staticmethod
    def _validate_episode(episode: EpisodicNode, event: EventAnalysisInput) -> None:
        if (
            episode.uuid != event.episode_uuid
            or event.episode_uuid != event_episode_uuid(event.event.id)
            or episode.group_id != GRAPHITI_GROUP_ID
            or episode.source_description != EVENT_SOURCE_DESCRIPTION
            or formal_event_id_from_content(episode.content) != event.event.id
        ):
            raise PermanentEventAnalysisFailure("Signal provenance requires the formal Event Episode")
