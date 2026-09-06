# Geopolitical storyline projection

Issue #153 replaces the nine graph-only demo blueprints with Data-owned storylines.
The operator joins Data PostgreSQL geopolitic_rivalries and geopolitic_domains using
sematica/initialization/geopolitic/export_snapshot.sql, freezes the JSON output,
and passes it to the projection CLI. This is a user-authorized local maintenance
export, not an AgentOS runtime SQL dependency. AgentOS receives no Data DB credentials.
A future periodic source refresh requires a Data-owned versioned API.

One GPR identity produces one Entity + GeopoliticRivalry node through the existing
Data-ID UUID5 rule. Each contains the Chinese name, classification, core proposition,
core actors, single domain ID/code/name/description, complete tactics, main transmission,
candidate assets, Data timestamps and projection ownership/fingerprint.
No separate domain or tactic node is created. Tactics are a JSON array encoded as a
string because Neo4j properties cannot store lists of objects; decoding reproduces
the exact ordered name/description objects. Candidate assets remain native string arrays.

Event-facing summary includes name, classification, domain, proposition, actors and
tactics. Main transmission and candidate assets remain separate post-match properties.
This change does not enforce a new Event matcher prompt or retrieval policy.
The existing Graphiti name_embedding convention is preserved: embed the name only.
Projection bypasses add_episode and LLM extraction and creates no relations.

Run inside the existing AgentOS environment:

```sh
python -m sematica.initialization.geopolitic.cli --snapshot /app/data/geopolitic/snapshot.json plan
python -m sematica.initialization.geopolitic.cli --snapshot /app/data/geopolitic/snapshot.json run
python -m sematica.initialization.geopolitic.cli --snapshot /app/data/geopolitic/snapshot.json verify
```

Plan validates required fields, IDs, join count, duplicate names, domain consistency,
tactics, assets and timestamps. A foreign, duplicate or stale geopolitical identity
fails before writing and requires an explicitly reviewed cleanup. The command never
deletes nodes or relationships. All embeddings are prepared before writing; an exact
replay skips embedding and writes. Verification checks the exact identity set, all
projected attributes and configured vector dimension. A write failure may be safely
retried with the same snapshot. Only one operator should run this CLI at a time.

The old catalog.v1.json is retained as a historical artifact and is no longer a CLI
default. Local cleanup is separately authorized, offline-backed maintenance. Full
rollback uses the pre-cleanup Neo4j dump and matching application checkout; never
delete named Neo4j volumes. Local development runs source through the mounted
checkout; release changes still require human PR merge.
