# Company Graph Projection

## Ownership and outcome

Tidewise Data owns Company and formal `CompanyIndustryLink` facts. AgentOS consumes the authenticated,
versioned `GET /api/data/v1/entities/companies` contract and never reads the business PostgreSQL database
directly. The projection creates canonical `Company` nodes and two provenance-separated classes of relations:

- `CompanyBelongsToIndustry`: authoritative Data fact backed by a `CIL...` record.
- `CompanyOperatesInIndustry`: bounded model inference to an existing canonical `IND...` node.
- `CompanyParticipatesInChainNode`: bounded model inference to an existing canonical `CND...` node.

Inference is a graph projection decision, not a new Data fact and not an investment conclusion.

## No-Episode write boundary

Company projection must never call Graphiti `add_episode` or its bulk variant. Episode ingestion performs
entity extraction and identity resolution and may create contextual entities that are outside this projection's
authority. This projection instead builds deterministic `EntityNode` and `EntityEdge` values, derives UUIDs
from canonical Data IDs, embeds only changed values, and writes them with direct Graphiti namespace bulk saves
and relationship `MERGE` operations.

Company is deliberately absent from the Event extraction entity whitelist. Existing Industry, IndustryChain,
and ChainNode nodes are read-only targets and are never created, relabeled, or updated by the Company writer.
Every projected Company node and relation carries `projection_owner=tidewise-agentos/company-projection/v1`.
An existing canonical `COM...` identity without that owner, the deterministic UUID, or exclusive
`Entity+Company` labels is treated as namespace pollution and stops the run.
The same preflight scans Company relation ownership in every direction and rejects duplicate UUIDs, reverse edges,
wrong endpoints, foreign owners, wrong labels, or a non-`RELATES_TO` relationship before direct `MERGE`.

## Candidate and decision policy

The model never sees or returns graph IDs. Code freezes a canonical target catalog, assigns short keys, and
accepts only those keys:

1. The first model call sees only the existing root Industries. Code then expands only the selected root subtrees,
   and a second call selects at most three direct operating Industries per Company. This keeps prompts bounded while
   preserving the complete canonical taxonomy. A weak name association is `LOW` confidence and creates no edge.
2. Code traverses only existing topology from each accepted Industry (including controlled descendants), through
   `IndustryChainMappedToIndustry`, to `ChainNodeBelongsToIndustryChain`.
3. The resulting ChainNode set is bounded and assigned `N1`, `N2`, ... keys. The model may select at most eight
   direct participation nodes for that Company.
4. Unknown key, duplicate or missing `input_index`, wrong target label, noncanonical UUID, no candidate, no match,
   and low confidence all fail closed or end without an inferred edge.

Each frozen Company decision records the exact root Industry, detailed Industry, and ChainNode candidate ID sets that
were hidden behind short keys. Resume and write reconstruct those sets from the unchanged catalog. The ChainNode
candidate bound is frozen in the run manifest and cannot change between resume calls.

The stored decision contains a concise rationale and supplied-field references, not hidden reasoning. It also
records the Company input fingerprint, target catalog fingerprint, ontology/policy/prompt versions, model ID,
confidence, source Industry IDs, and decision time.

## Run lifecycle

All commands use the same explicit run directory. The directory contains an immutable manifest and one atomic
decision file per Company. A nonblocking lock prevents concurrent inference or sweep for the same run.

```bash
source .venv/bin/activate

python -m sematica.projection.company_cli \
  --env-file .runtime/graphiti.env \
  --run-dir data/company-projection/runs/<run-id> \
  plan

# A bounded smoke decision; this never writes Neo4j.
python -m sematica.projection.company_cli \
  --env-file .runtime/graphiti.env \
  --run-dir data/company-projection/runs/<run-id> \
  infer --limit 1

# Resume until decisions_complete is true. Omitting --limit processes all remaining Companies.
python -m sematica.projection.company_cli \
  --env-file .runtime/graphiti.env \
  --run-dir data/company-projection/runs/<run-id> \
  infer

python -m sematica.projection.company_cli \
  --env-file .runtime/graphiti.env \
  --run-dir data/company-projection/runs/<run-id> \
  run --replace

python -m sematica.projection.company_cli \
  --env-file .runtime/graphiti.env \
  --run-dir data/company-projection/runs/<run-id> \
  verify
```

Inside the Compose service, the CLI also accepts the existing `DATA_SERVICE_BASE_URL` and
`DATA_SERVICE_TOKEN` environment names, so the same phases can be run as
`docker compose exec -T agentos python -m sematica.projection.company_cli --run-dir ... <phase>` without
copying secrets into a second file. Host-side standalone execution uses the explicit private env file.

- `plan` reads Data and Neo4j, freezes both fingerprints, validates formal targets, and performs no model or graph write.
- `infer` freezes model decisions before graph mutation. A retry reuses completed Company files and makes no repeat
  model call for them.
- `run --replace` requires every Company to have a terminal decision. It writes changed nodes and edges first and
  only then removes explicit stale canonical UUIDs that passed the projection-owner preflight. It never sweeps by
  the `Company` label alone.
- `verify` proves exact Company ID parity with the Data snapshot, exact formal/inferred edge parity, exclusive labels,
  deterministic UUIDs, configured embedding dimension, and unchanged canonical target fingerprint.

An identical rerun is expected to make zero model calls, zero embedding calls, and zero semantic graph writes.
`NO_CANDIDATE`, `NO_MATCH`, and `LOW_CONFIDENCE` remain auditable terminal outcomes and never force a relationship.

## Failure and rollback boundary

A Data snapshot change, cursor drift, catalog change, incomplete journal, wrong target, or provider failure stops the
run before sweep. A graph-write failure can be retried from frozen decisions. Rollback is limited to nodes with the
owned projection marker, canonical `COM...` identity, deterministic UUID, and relations named
`CompanyBelongsToIndustry`, `CompanyOperatesInIndustry`, or `CompanyParticipatesInChainNode`; it must not delete
foreign/contextual Company nodes or mutate Industry, IndustryChain, or ChainNode targets.

The initial full catalog has 13,264 Companies. With a model batch of 20, the upper-level classification requires
about 664 batches; ChainNode classification is another bounded stage for Companies with accepted Industries.
Company node writes use batches of 100, while the configured embedder currently chunks requests in tens. Operators
should use `infer --limit` for the first live smoke and inspect the frozen rationale before resuming the complete run.
