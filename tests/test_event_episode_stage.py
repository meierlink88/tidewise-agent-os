"""Stateful side-effect recovery tests for the production Event Episode stage."""

import json
import unittest
from unittest.mock import patch

from graphiti_core.nodes import EpisodicNode

from sematica.ingestion.episcode.event.contracts import HistoricalEvent
from sematica.ingestion.episcode.event.provenance import (
    EVENT_SOURCE_DESCRIPTION,
    PENDING_EVENT_SOURCE_DESCRIPTION,
    event_episode_uuid,
)
from sematica.ingestion.episcode.event.stages.episode import GraphitiEpisodeStage


class StatefulDriver:
    """Minimal query seam retaining the graph state observed by the Stage."""

    _database = "neo4j"

    def __init__(self) -> None:
        self.episode: dict[str, object] | None = None
        self.counts = {"event_episodes": 0, "mentions": 0, "ordinary_facts": 0, "signal_facts": 0}
        self.contextual_entity_isolation_calls = 0
        self.entities = [
            {
                "uuid": "contextual-chain",
                "group_id": "neo4j",
                "labels": {"Entity", "IndustryChain"},
                "data_object_id": None,
            },
            {
                "uuid": "catalog-chain",
                "group_id": "neo4j",
                "labels": {"Entity", "IndustryChain"},
                "data_object_id": "ICH-authoritative",
            },
            {
                "uuid": "foreign-contextual-chain",
                "group_id": "another-tenant",
                "labels": {"Entity", "IndustryChain"},
                "data_object_id": None,
            },
        ]

    async def execute_query(self, query: str, **parameters):
        if "graphiti_event_projection_identity" in query:
            if self.episode is None:
                return [], None, None
            return [dict(self.episode, mention_count=self.counts["mentions"])], None, None
        if "graphiti_native_event_metadata" in query:
            assert self.episode is not None
            self.episode.update(
                {
                    "name": parameters["title"],
                    "source_description": parameters["source_description"],
                    "episode_kind": "EVENT",
                    "domain_object_id": parameters["event_id"],
                }
            )
            return (
                [
                    {
                        "uuid": self.episode["uuid"],
                        "episode_kind": self.episode["episode_kind"],
                        "domain_object_id": self.episode["domain_object_id"],
                    }
                ],
                None,
                None,
            )
        if "graphiti_event_contextual_entity_isolation" in query:
            self.contextual_entity_isolation_calls += 1
            assert "entity:Entity {group_id: $group_id}" in query
            isolated = 0
            for entity in self.entities:
                labels = entity["labels"]
                if (
                    entity["group_id"] == parameters["group_id"]
                    and not entity["data_object_id"]
                    and not entity.get("demo_catalog_key")
                    and not entity.get("policy_key")
                    and labels.intersection(parameters["controlled_labels"])
                ):
                    entity["contextual_entity_type"] = next(
                        label for label in parameters["controlled_labels"] if label in labels
                    )
                    labels.add("ContextualEntity")
                    labels.difference_update({"Country", "Region", "Concept", "IndustryChain", "ChainNode"})
                    isolated += 1
            return [{"isolated_count": isolated}], None, None
        raise AssertionError("unexpected Stage query")


class StatefulGraphiti:
    """Graphiti seam that loses the first ACK after its atomic bulk write."""

    def __init__(self, driver: StatefulDriver) -> None:
        self.driver = driver
        self.add_episode_calls = 0

    async def add_episode(self, **parameters):
        del parameters
        self.add_episode_calls += 1
        self.driver.counts["mentions"] += 3
        self.driver.counts["ordinary_facts"] += 2
        raise ConnectionError("acknowledgement lost after Graphiti bulk write")


class GraphitiEpisodeStageTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def historical() -> HistoricalEvent:
        return HistoricalEvent.model_validate(
            {
                "id": "EVT15bec7e3-998c-5434-aa5d-29712c4c67cf",
                "event": {
                    "title": "示例公司签署服务器订单",
                    "summary": "示例公司宣布签署服务器订单。",
                    "semantic": {
                        "actors": ["示例公司"],
                        "action": "签署",
                        "objects": ["服务器订单"],
                        "stage": "ANNOUNCED",
                        "modality": "FACT",
                        "time": {
                            "occurred_at": None,
                            "announced_at": "2026-08-25T00:00:00Z",
                            "effective_at": None,
                            "precision": "DAY",
                        },
                        "jurisdictions": ["中国"],
                        "reason": "客户扩容需求",
                        "method": "公告宣布",
                        "metrics": [
                            {
                                "name": "订单金额",
                                "value": "100",
                                "unit": "亿元",
                                "change": None,
                                "period": "2026年",
                            }
                        ],
                    },
                },
            }
        )

    async def test_lost_add_episode_ack_is_finalized_without_duplicate_graph_writes(self) -> None:
        historical = self.historical()
        driver = StatefulDriver()
        graphiti = StatefulGraphiti(driver)
        stage = GraphitiEpisodeStage(graphiti)  # type: ignore[arg-type]

        async def save_pending(node: EpisodicNode, target: StatefulDriver) -> None:
            self.assertIs(target, driver)
            driver.episode = {
                "uuid": node.uuid,
                "name": node.name,
                "content": node.content,
                "source_description": node.source_description,
                "episode_kind": None,
                "domain_object_id": None,
            }
            driver.counts["event_episodes"] += 1

        with patch.object(EpisodicNode, "save", new=save_pending):
            with self.assertRaises(ConnectionError):
                await stage.execute(historical)
            counts_after_bulk_write = driver.counts.copy()
            episode_uuid = await stage.execute(historical)

        self.assertEqual(episode_uuid, event_episode_uuid(historical.id))
        self.assertEqual(graphiti.add_episode_calls, 1)
        self.assertEqual(driver.contextual_entity_isolation_calls, 1)
        entities = {str(entity["uuid"]): entity for entity in driver.entities}
        self.assertEqual(entities["contextual-chain"]["labels"], {"Entity", "ContextualEntity"})
        self.assertEqual(entities["contextual-chain"]["contextual_entity_type"], "IndustryChain")
        self.assertIn("IndustryChain", entities["catalog-chain"]["labels"])
        self.assertNotIn("ContextualEntity", entities["catalog-chain"]["labels"])
        self.assertIn("IndustryChain", entities["foreign-contextual-chain"]["labels"])
        self.assertNotIn("ContextualEntity", entities["foreign-contextual-chain"]["labels"])
        self.assertEqual(driver.counts, counts_after_bulk_write)
        self.assertEqual(
            driver.counts,
            {"event_episodes": 1, "mentions": 3, "ordinary_facts": 2, "signal_facts": 0},
        )
        assert driver.episode is not None
        self.assertEqual(driver.episode["source_description"], EVENT_SOURCE_DESCRIPTION)
        self.assertNotEqual(driver.episode["source_description"], PENDING_EVENT_SOURCE_DESCRIPTION)
        self.assertEqual(driver.episode["domain_object_id"], historical.id)
        content = json.loads(str(driver.episode["content"]))
        self.assertEqual(content["id"], historical.id)
        self.assertEqual(set(content), {"id", "status", "title", "summary", "semantic"})
        self.assertEqual(
            set(content["semantic"]),
            {
                "actors",
                "action",
                "objects",
                "stage",
                "modality",
                "time",
                "jurisdictions",
                "reason",
                "method",
                "metrics",
            },
        )
        self.assertNotIn("attribution", content["semantic"])


if __name__ == "__main__":
    unittest.main()
