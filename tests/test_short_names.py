"""Short-name contracts and fail-closed property update planning."""

import importlib
import os
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from sematica.ontology.entities.chain_node import ChainNode
from sematica.ontology.entities.geopolitic_rivalry import GeopoliticRivalry
from sematica.ontology.entities.industry_chain import IndustryChain
from sematica.ontology.entities.macro_economic import MacroEconomic
from sematica.projection.authoritative_writer import node_uuid
from sematica.projection.short_names import PREFIXES, Package, Record, Snapshot, check, prepare


def fixture():
    rows = [Record(entity_type=k, id=v + str(uuid4()), name=k, short_name="简称") for k, v in PREFIXES.items()]
    source = Snapshot(source="test", exported_at=datetime.now(UTC), rows=rows)
    state = {r.id: {"uuid": node_uuid(r.id), "name": r.name, "name_embedding": [0.1, 0.2]} for r in rows}
    return source, state


class ShortNamesTest(unittest.TestCase):
    def test_nullable_bounded_authoritative_name(self):
        for model in [ChainNode, IndustryChain, MacroEconomic, GeopoliticRivalry]:
            for value in [None, "AI芯片", "五个字简称"]:
                self.assertEqual(model(short_name=value).short_name, value)
            for value in ["", "   ", "六个汉字的简称"]:
                with self.assertRaises(ValidationError):
                    model(short_name=value)

    def test_plan_preserves_identity_and_checks_drift(self):
        source, state = fixture()
        plan = prepare(source, state, "test")
        self.assertEqual(len(plan.rows), 4)
        check(plan, state, after=False)
        for r in source.rows:
            state[r.id]["short_name"] = r.short_name
        check(plan, state, after=True)
        state[source.rows[0].id]["name_embedding"] = [0.9]
        with self.assertRaises(ValueError):
            check(plan, state, after=True)

    def test_missing_report_and_name_conflict(self):
        source, state = fixture()
        del state[source.rows[0].id]
        self.assertEqual(len(prepare(source, state, "test").missing), 1)
        state[source.rows[1].id]["name"] = "Changed"
        with self.assertRaises(ValueError):
            prepare(source, state, "test")

    def test_duplicate_source_and_tampered_package_rejected(self):
        source, state = fixture()
        with self.assertRaises(ValidationError):
            Snapshot(source="test", exported_at=datetime.now(UTC), rows=[*source.rows, source.rows[0]])
        plan = prepare(source, state, "test").model_dump(mode="json")
        plan["rows"][0]["short_name"] = "篡改"
        with self.assertRaises(ValidationError):
            Package.model_validate(plan)

    def test_null_clears_existing_short_name(self):
        source, state = fixture()
        data = source.model_dump()
        data["rows"][0]["short_name"] = None
        source = Snapshot.model_validate(data)
        state[source.rows[0].id]["short_name"] = "旧名"
        plan = prepare(source, state, "test")
        self.assertEqual(plan.rows[0].before, "旧名")
        for r in source.rows:
            state[r.id]["short_name"] = r.short_name
        del state[source.rows[0].id]["short_name"]
        check(plan, state, after=True)


class ProjectionMappingTest(unittest.TestCase):
    def test_chain_dtos_and_node_attributes(self):
        node = importlib.import_module("sematica.initialization.chainnode.projection")
        chain = importlib.import_module("sematica.projection.industry_chain")

        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        common = dict(
            name="正式名称", short_name="简称", aliases=[], review_status="approved", created_at=stamp, updated_at=stamp
        )
        n = node.DataChainNodeDTO(id="CND" + str(uuid4()), definition="定义", **common)
        nplan = node.build_plan(node.ChainNodeFacts(chain_nodes=(n,), memberships=(), graph_edges=()))
        self.assertEqual(nplan.nodes[0].attributes["short_name"], "简称")
        c = chain.DataIndustryChainDTO(
            id="ICH" + str(uuid4()),
            scope="范围",
            target_output="产出",
            end_use="用途",
            geography="中国",
            primary_country_id=None,
            as_of_date=stamp.date(),
            review_note=None,
            technology_route_qualifier=None,
            observable_variables=["订单"],
            **common,
        )
        industry_id = "IND" + str(uuid4())
        entity = chain.DataResearchEntityDTO(
            entity_id=industry_id,
            entity_type="industry",
            name="行业",
            canonical_name="行业",
            aliases=[],
            status="active",
        )
        mapping = chain.DataMappingDTO(
            entity_relation_id="ERL" + str(uuid4()),
            from_entity_id=c.id,
            to_entity_id=industry_id,
            relation_type="mapped_to_industry",
            status="active",
        )
        cplan = chain.build_plan(
            chain.IndustryChainFacts(industry_chains=(c,), entities=(entity,), mappings=(mapping,))
        )
        self.assertEqual(cplan.nodes[0].attributes["short_name"], "简称")

    def test_storyline_snapshots_accept_and_project_names(self):
        from sematica.initialization.geopolitic import projection as geo
        from sematica.initialization.macroeconomic import projection as macro

        for module, prefix, domain, extra in [
            (
                geo,
                "GPR",
                "geopolitic_domain_id",
                {"category": "类别", "core_actors": "主体", "main_transmission": "传导"},
            ),
            (macro, "MEC", "macro_economics_domain_id", {}),
        ]:
            row: dict[str, object] = dict(
                id=prefix + str(uuid4()),
                name="名称",
                short_name="简称",
                domain_code="TEST",
                domain_name="领域",
                domain_description="说明",
                tactics=[{"name": "手段", "description": "说明"}],
                core_proposition="命题",
                candidate_assets=["资产"],
                **extra,
            )
            row[domain] = ("GPD" if module is geo else "MCD") + str(uuid4())
            for key in ["created_at", "updated_at", "domain_created_at", "domain_updated_at"]:
                row[key] = datetime(2026, 1, 1, tzinfo=UTC)
            parsed = module.Storyline.model_validate(row)
            self.assertEqual(parsed.ontology().short_name, "简称")
            row.pop("short_name")
            self.assertIsNone(module.Storyline.model_validate(row).ontology().short_name)


@unittest.skipUnless(os.getenv("RUN_SHORT_NAMES_NEO4J_TEST") == "1", "requires local Neo4j")
class Neo4jTransactionTest(unittest.TestCase):
    def test_property_update_and_rollback_in_discarded_transaction(self):
        from neo4j import GraphDatabase

        from sematica.projection.short_names import apply, inspect

        source, _ = fixture()
        with GraphDatabase.driver(
            os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"])
        ) as d:
            with d.session(database="neo4j") as session:
                tx = session.begin_transaction()
                try:
                    for r in source.rows:
                        tx.run(
                            f"CREATE (n:Entity:{r.entity_type}) SET n = $props",
                            props={
                                "uuid": node_uuid(r.id),
                                "data_object_id": r.id,
                                "group_id": "neo4j",
                                "name": r.name,
                                "name_embedding": [0.1, 0.2],
                                "short_name": "旧名",
                            },
                        ).consume()
                    ids = [node_uuid(r.id) for r in source.rows]
                    tx.run(
                        "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
                        "CREATE (a)-[:TEST_SHORT_NAMES {value:7}]->(b)",
                        a=ids[0],
                        b=ids[1],
                    ).consume()
                    plan = prepare(source, inspect(tx, source.rows), "local-test")
                    apply(tx, plan)
                    check(plan, inspect(tx, source.rows), after=True)
                    apply(tx, plan, rollback=True)
                    check(plan, inspect(tx, source.rows), after=False)
                    self.assertEqual(
                        tx.run(
                            "MATCH (:Entity {uuid:$id})-[e:TEST_SHORT_NAMES]->() RETURN e.value AS v", id=ids[0]
                        ).value("v")[0],
                        7,
                    )
                    data = source.model_dump()
                    data["rows"][0]["short_name"] = None
                    clear_source = Snapshot.model_validate(data)
                    clear_plan = prepare(clear_source, inspect(tx, source.rows), "local-test")
                    apply(tx, clear_plan)
                    self.assertNotIn("short_name", inspect(tx, source.rows)[source.rows[0].id])
                    tx.run("MATCH (n:Entity {uuid:$id}) SET n.name='drift'", id=ids[0]).consume()
                    with self.assertRaises(ValueError):
                        apply(tx, clear_plan, rollback=True)
                finally:
                    tx.rollback()
                self.assertEqual(
                    session.run("MATCH (n:Entity) WHERE n.uuid IN $ids RETURN count(n) AS n", ids=ids).value("n")[0], 0
                )


if __name__ == "__main__":
    unittest.main()
