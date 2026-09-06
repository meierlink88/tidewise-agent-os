# Macroeconomic storyline projection

Issue #159 replaces the historical policy-action demo with Data-owned macroeconomic storylines.
Data PR #418 owns `macro_economics` and `macro_economics_domain`. No Data API, PG schema or runtime
database access is introduced here. An operator exports a single consistent joined snapshot with
`sematica/initialization/macroeconomic/export_snapshot.sql`. The source count detects lossy joins.
The current approved dataset has 34 storylines across 10 domains; these counts are not hardcoded.

## Graph contract

One MEC ID becomes one `Entity:MacroEconomic` node with the shared Data-ID UUID5 identity rule.
Graphiti owns `uuid`, `name`, `group_id`, `summary`, `created_at` and `name_embedding`.
The ontology owns `data_object_id`, `core_proposition`, `domain_code`, `domain_name`,
`domain_description`, `tactics`, `candidate_assets` and `updated_at`.
Projection adds its owner, source fingerprint and domain timestamps for parity and replay.
The PG domain FK is used for input consistency only, not projected.

`tactics` is JSON-array text with ordered `{name, description}` objects because Neo4j cannot
store a list of maps as a property. `candidate_assets` is a native ordered string array.
No separate domain/tactic nodes or inferred links are created. `CountryImplementsMacroEconomic`
is removed from the ontology: policy-tool applicability is not a storyline relation.
The historical `catalog.v1.json` remains for audit only; the CLI requires an explicit snapshot
and cannot silently fall back to the old policy-action catalog.

Summary contains name, domain profile, core proposition and reference tactics. Candidate assets
are excluded. The proposition is an impact mechanism, not evidence that an Event happened or
that a downstream asset is directly affected. Existing name-only embedding is preserved.
This change does not alter Event matching prompts or generate investment conclusions.

## Operator flow

Run with the existing AgentOS environment and approved local Neo4j target:

```sh
python -m sematica.initialization.macroeconomic.cli --snapshot /app/data/macroeconomic/snapshot.json plan
python -m sematica.initialization.macroeconomic.cli --snapshot /app/data/macroeconomic/snapshot.json run
python -m sematica.initialization.macroeconomic.cli --snapshot /app/data/macroeconomic/snapshot.json verify
```

Plan validates identity, fields, full tactics, assets, timestamps and domain consistency without
network access. Run rejects old, foreign, duplicate or stale identities before writes. All embeddings
are prepared before writing. Exact replay performs no embedding calls or writes. A partial write
can be retried with the identical snapshot; verify requires exact identity/property/vector parity.
Only one operator should run the projector at a time; it never deletes anything.

Replacement is separate, explicitly authorized local maintenance: coordinate graph writers,
inventory target identities/labels/incident edges, create a recoverable backup, delete only the
validated MacroEconomic set and incident edges, project and compare non-target graph state.
Do not delete named volumes. Full rollback uses the backup and matching old application version.
Do not treat removed Signal links as new storyline evidence or recreate inferred links.
Application delivery follows human PR merge; avoid mixed-version graph writers during replacement.

## Validation and review

The authoritative projection seam tests validation, namespace collision, exact parity and replay;
the geopolitical seam remains unchanged. Ontology contract tests cover retirement of policy-action
fields and relation registration. Real local verification compares the joined PG snapshot to Neo4j.
The shared development workflow prohibits delegation without explicit user request, so the
code-review skill's Standards and Spec axes are performed by the current task, not subagents.
This exception changes review execution only, not data contracts or acceptance requirements.
