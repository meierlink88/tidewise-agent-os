# Raw Collection Workflow

## Outcome

Raw Collection uses an Agent for query planning and three stable Tool façades for acquisition, then deterministic
Workflow Functions build and publish immutable Artifacts:

```text
CollectionRequest
  -> plan-collection-query           -> CollectionQueryPlan only
  -> execute-collection-channels
       -> web_fetch implementation   -> one enabled Web Search channel
       -> api_fetch implementation   -> all enabled API channels, bounded concurrent
       -> rss_fetch implementation   -> all enabled RSS/Atom channels, bounded concurrent
  -> prepare-title-curation          -> candidate_id and title only
  -> curate-collection-titles        -> candidate_id and strict is_relevant boolean
  -> validate-title-curation         -> exact Candidate coverage
  -> build-artifact-set
  -> publish-collection            -> MinIO Markdown objects, then local manifest
  -> CollectionResult
```

The Workflow freezes an immutable enabled-channel snapshot before the Agent plans the query. Complete provider results
go directly to the run-scoped Collection Buffer; the model never receives or reconstructs source content.

Title Curator is a conservative binary pre-filter. Clearly relevant titles receive `is_relevant=true`; clearly
irrelevant titles receive `false`; title-only ambiguity is retained as `true` so potentially important source material
is not discarded before full-text Evidence Extraction. The Agent does not generate open-ended reason codes.

## Channel catalog

`collection_channels` lives in the existing AgentOS PostgreSQL database. Every row contains:

- immutable `code` for channel identity;
- editable `name` for operators;
- `ownership_type`: `fixed` or `dynamic`;
- `channel_type`: `web_search`, `api`, or `rss`;
- `adapter_key` selecting code-owned protocol behavior;
- `enabled`, `endpoint`, plaintext `app_key`, and provider `config`;
- `priority`, `timeout_seconds`, and `max_results`;
- `default_source_level`: `L1_OFFICIAL`, `L2_WIRE`, `L3_MEDIA`, or `L4_SOCIAL`;
- creation and update timestamps.

Priority 1 is highest. A database constraint allows at most one enabled Web Search row. Fixed rows cannot be deleted,
but they may be disabled or reconfigured. Dynamic rows may be added and deleted. Standard RSS and Atom rows all use
the `generic_rss` Adapter.

Startup creates the table and inserts only missing fixed rows. Search Key and endpoint environment variables provide
first-seed values; startup never overwrites an existing row. Database edits therefore affect the next Workflow run
without a restart.

## Adapter seam

The database owns channel instances and operational settings. Python owns incompatible provider protocols. An Adapter
accepts one validated channel plus one common fetch request and returns normalized `Candidate` values.

Initial Adapter keys are:

- Web Search: `bocha`, `tavily`, `parallel`;
- structured API: `cls`, `eastmoney_fast`, `eastmoney_stock`, `stcn`;
- dynamic feed: `generic_rss`.

Tavily requests `include_raw_content: "text"`; AgentOS does not accept Tavily's provider-generated Markdown.
Bocha, Parallel, the structured APIs and RSS/Atom have no equivalent Markdown-format selector in their current
protocols and already normalize provider text, snippets or feed content before Artifact construction. AgentOS is the
single owner of the persisted Markdown wrapper and YAML frontmatter.

Adding another instance of a supported protocol is a database operation. Adding an incompatible protocol still needs a
reviewed Adapter. Web Search candidates resolve source trust from the actual result host when `config.source_levels`
contains a matching domain; otherwise they inherit the channel default.

## Time contract

The Agent derives one integer `lookback_hours` from the objective, defaulting to 48. The Workflow captures one UTC
`collector_cutoff` before model execution. The deterministic acquisition Function invokes all three shared
implementations with the same plan and derives:

```text
published_before = collector_cutoff
published_after  = collector_cutoff - lookback_hours
```

There is no time-window Tool and the Agent never supplies absolute interval strings. Providers receive the interval when
their protocol supports it; deterministic Artifact construction always enforces it.

## Prompt and runtime ownership

- Collector business instructions remain a published Agno Studio component in PostgreSQL.
- Title Curator's binary output schema and conservative retention rules are contract-bound. A contract migration
  republishes the reviewed seed prompt so its Instructions and Pydantic schema cannot drift across the same version.
- Code owns the query-plan schema, runtime contract, model wiring, adapters, catalog and Artifact invariants.
- A runtime contract migration may publish a new component version while preserving operator-managed Instructions
  byte-for-byte. Obsolete provider-specific Tool instructions are superseded through code-owned additional context.
- Raw Collection Workflow graph versions remain Studio-managed; Python Function executors remain Git-managed.
- The Workflow exclusively persists the durable Agno session. Studio-managed Collector and Title Curator components
  are rehydrated as database-free runtime copies only when embedded in Workflow Steps, so their outputs remain under
  Workflow step details without competing Agent session writes. Direct Agent runs retain their configured database.

## Invariants

- The Agno Workflow Run ID is the only collection identity.
- Every acquisition façade in one run uses the same frozen channel snapshot, cutoff and requested lookback.
- `web_fetch` fails closed if the catalog exposes more than one enabled Web Search row.
- `api_fetch` and `rss_fetch` execute enabled channels in stable priority/code order with bounded concurrency.
- One provider failure does not discard successful sibling results; raw provider errors and keys never enter receipts or
  Artifacts.
- Every Candidate preserves channel code, query, URL, original publisher, effective source level, direct content,
  publication-time hint and collection time.
- Every Candidate receives exactly one strict boolean title-relevance decision. Irrelevant candidates use the
  deterministic local audit reason `title_irrelevant`; no model-generated reason taxonomy is persisted.
- Every successful channel call writes a complete Tool Batch before returning its receipt.
- Every Candidate reaches exactly one deterministic terminal disposition before publication.
- Manifest publication is last; downstream Workflows consume `indexes/manifest-index.jsonl` by byte offset.
- Before a manifest becomes visible, every accepted Markdown document exists as an immutable matching object in the
  configured MinIO `raw-evidence` bucket.
- Object keys are content-addressed by the complete Markdown SHA-256, so concurrent publications with different bytes
  cannot target the same key; identical bytes converge on the same immutable object.
- A document manifest records `url_path=/{bucket}/{object_key}` without a scheme, host or port.
- The URL path freezes the bucket at build time; publication recovers the upload bucket from that prepared identity
  rather than reading mutable environment configuration again.
- Published files remain under the Git-ignored `data/collector/` root so Evidence AI input does not depend on MinIO.

## Failure semantics

- Missing provenance, cutoff, invalid query, or invalid lookback produces a stable safe Tool error and no write.
- Missing Adapter, provider timeout, invalid response, or request failure is isolated to that channel.
- A façade with no enabled channels returns `no_channels`; sibling façades still execute.
- If no Tool Batch exists after deterministic acquisition, Artifact construction fails rather than publishing false success.
- Build or MinIO failure leaves the final manifest and manifest index unpublished; publication is idempotent for
  immutable matching local files and MinIO objects.
- A matching MinIO object size and SHA-256 metadata is a retry success; a different object under the same key fails.
- Workflow steps use no hidden retries and fail on deterministic Function errors.

## Acceptance seam

Acceptance is observed at the three Tool interfaces and the complete Workflow:

- catalog state selects the expected channels;
- Adapter payloads normalize to Candidate contracts;
- concurrent fan-out isolates partial failure;
- all Tool Batches share one exact interval;
- Artifact metadata carries channel and source level;
- Registry exposes only `web_fetch`, `api_fetch`, and `rss_fetch`;
- REST, MCP, health and the full validation suite remain green.
