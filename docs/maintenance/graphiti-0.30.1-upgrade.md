# Graphiti Core 0.30.1 compatibility assessment

Checked on 2026-09-08 against AgentOS main `ee29a3d`; tracked by Issue #215.

## Decision and scope

Upgrade `graphiti-core` from 0.29.3 to 0.30.1. No application adapter change is
required by the inspected package changes or the checks below. This prepares the
upgrade for human review; UAT deployment is outside this change.

`requirements.txt` is the repository lockfile of record. Its only package change
is Graphiti Core. The ignored local `uv.lock` was refreshed but is not a PR artifact.
The existing untracked `outputs/` artifacts were preserved.

## Upstream comparison

Sources:

- https://pypi.org/project/graphiti-core/0.30.1/ (published 2026-09-01)
- https://github.com/getzep/graphiti/releases/tag/v0.30.0

The GitHub release title says 0.30.1 while its tag is v0.30.0; this assessment
compared the actual PyPI 0.29.3 and 0.30.1 wheels, not the release title alone.
Python support (`>=3.10,<4`) and all dependency requirements are identical.
AgentOS uses Python 3.12, Neo4j Python driver 6.2.0 and Pydantic 2.13.4.

Only three Python files under `graphiti_core` differ:

| Change | AgentOS impact |
| --- | --- |
| `driver/neo4j_driver.py`: correctly pass `database_` to the Neo4j client; limit error logs to parameter keys; route index deletion through the driver | Live query and session paths both resolve to `neo4j`. Local Compose uses Neo4j 5.26.28 Community, with no custom database configured by the project factory. No data move is indicated for this local topology. |
| `search/search.py`: use RRF to select up to twice the requested limit before edge cross-encoder reranking | Current explicit Event/company/investment node search recipes use node RRF. Edge cross-encoder users may see different ranking and more reranker calls; ranking equality is not promised. |
| `utils/maintenance/node_operations.py`: preserve existing attributes when no entity type applies | Prevents clearing attributes during native extraction. The current deterministic Event association path does not depend on free entity extraction. |

Graphiti construction, Agno LLM/cross-encoder interfaces, embedder interfaces,
node/edge/episode models, repository operations and search configuration modules
are unchanged in the wheel comparison. No index schema or embedding construction
change was found; this upgrade does not call for graph rebuilding or re-embedding.
Custom-database UAT/production targets must independently verify query/session
routing before rollout, as described in the upstream release notes.

## Verification

- All 18 distinct Graphiti symbols imported by `sematica` resolve under 0.30.1;
  the wheel comparison found no added or removed Python modules.

- Installed requirements plus refreshed editable project metadata: `uv pip check`
  passes for all 127 local packages. Other requirement pins remain unchanged.
- `source .venv/bin/activate`; `./scripts/format.sh`; `./scripts/validate.sh`:
  184 files unchanged by format, lint passes, mypy passes for 89 source files.
- A temporary probe loaded the new PyPI wheel in the existing container, using
  the project's Graphiti factory, real Neo4j and configured model/embedding services.
  Construction occurred outside the event loop to avoid the SDK's automatic
  index initialization task during this probe.
- Query and session database names both equal `neo4j`; all 33 indexes are ONLINE.
- Existing entity deserialization and combined BM25/vector/node-RRF search pass
  (five results in the expected group). Configured embedding dimension is verified.
- Actual `AgnoGraphitiLLM.generate_response` with a Pydantic schema and
  `AgnoGraphitiReranker.rank` calls pass against the configured model.
- In one explicitly rolled-back Neo4j transaction, the project's `write_projection`
  embeds and bulk-saves two synthetic entities, upserts their relationship, and
  reads entity attributes and the relationship back through SDK repositories.
  Episode save/read also passes. The probe redirects the client's query execution
  into that transaction; ordinary query routing is checked separately above.
- After rollback, a separate query confirms zero nodes in the unique probe group.

The repository previously removed its test suite in `cdf27a3`; no deleted test
suite or CI test runner is restored. These are focused compatibility probes, not
an end-to-end replay of collection, Event extraction or investment publication.
They do not establish unchanged model quality or retrieval ranking on all inputs.

## Rollout and rollback

The local image build succeeded and `agent-os-service` now reports Graphiti Core
0.30.1. `/health` returned OK; REST `local-ping` completed with `Tidewise AgentOS OK`.
`./scripts/mcp_check.sh` passed its handshake, eight-tool listing, registered
component check and real assistant call (7.6 seconds). Five local schedules were
disabled before the rollout check. Persisted RUNNING records
predate the current container start and were left untouched.

For a later environment, install the reviewed requirements, rebuild its image,
verify the SDK version, health, configured database and MCP behavior before
resuming workloads. Preserve schedule configuration and existing Neo4j volumes.

For local rollback, the pre-upgrade image is retained as
`tidewise-agent-os:pre-graphiti-0301-215` (image ID
`sha256:93f74b9fdcbee2f252b39f3292b298220f7097e66f9a1cc6cfa45b0e21c031e4`).
Run `IMAGE_TAG=pre-graphiti-0301-215 docker compose up -d --no-build agentos` to
restore it without removing volumes. For a durable rollback, revert the two
tracked dependency pins through a reviewed PR and rebuild. No graph migration
was performed by the compatibility probe.
