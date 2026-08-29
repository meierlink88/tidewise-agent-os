# Raw Collection Workflow

## Outcome

Raw Collection uses an Agent for query planning and one deterministic Function for acquisition, then deterministic
Workflow Functions build and publish immutable Artifacts:

```text
CollectionRequest
  -> prepare-collection-context      -> validated objective only
  -> plan-collection-query           -> CollectionQueryPlan only
  -> execute-collection-channels
       -> freeze Source Snapshot and UTC cutoff
       -> Web Search group           -> one enabled Web Search channel
       -> API group                  -> all enabled API channels, bounded concurrent
       -> RSS group                  -> all enabled RSS/Atom channels, bounded concurrent
  -> prepare-title-curation          -> candidate_id and title only
  -> curate-collection-titles        -> candidate_id and strict is_relevant boolean
  -> validate-title-curation         -> exact Candidate coverage
  -> build-artifact-set
  -> publish-collection            -> MinIO Markdown objects, then local manifest
  -> CollectionResult
```

The acquisition Function freezes one immutable enabled-channel snapshot and UTC cutoff after the Agent plans the query.
Complete provider results go directly to the run-scoped Collection Buffer; the model never receives or reconstructs
source content.

Title Curator is a conservative binary pre-filter. Clearly relevant titles receive `is_relevant=true`; clearly
irrelevant titles receive `false`; title-only ambiguity is retained as `true` so potentially important source material
is not discarded before full-text Evidence Extraction. The Agent does not generate open-ended reason codes.

## Source Snapshot

Data Service is the only Source management and persistence authority. During every Raw Collection acquisition Step,
AgentOS calls
the versioned `GET /api/data/v1/source-snapshot` contract exactly once with its service token. The complete active
Snapshot contains:

- immutable `code` for channel identity;
- editable `name` for operators;
- `ownership_type`: `fixed` or `dynamic`;
- `channel_type`: `web_search`, `api`, or `rss`;
- `adapter_key` selecting code-owned protocol behavior;
- `enabled`, `endpoint`, plaintext `app_key`, and provider `config`;
- `priority`, `timeout_seconds`, and `max_results`;
- `default_source_level`: `L1_OFFICIAL`, `L2_WIRE`, `L3_MEDIA`, or `L4_SOCIAL`;
- creation and update timestamps.

Priority 1 is highest. The consumer requires an active-only, unique and stably ordered complete Snapshot with at most
one Web Search Source. Empty Snapshot is valid. AgentOS maps it onto the existing immutable Collection Channel execution
model and freezes it before channel execution. Fetch, authorization, timeout, size or integrity failure aborts acquisition;
AgentOS never truncates, drops an item, uses a partial response, caches a previous response or falls back to its legacy
table. The legacy table is retained only for rollback to an older image and is not read or mutated by current code.

## Adapter seam

Data Service owns Source instances and operational settings. Python owns incompatible provider protocols. An Adapter
accepts one validated channel plus one common fetch request and returns normalized `Candidate` values.

Initial Adapter keys are:

- Web Search: `bocha`, `tavily`, `parallel`;
- structured API: `cls`, `eastmoney_fast`, `eastmoney_stock`, `stcn`;
- dynamic feed: `generic_rss`.

Tavily requests `include_raw_content: "text"`; AgentOS does not accept Tavily's provider-generated Markdown.
Bocha, Parallel, the structured APIs and RSS/Atom have no equivalent Markdown-format selector in their current
protocols and already normalize provider text, snippets or feed content before Artifact construction. AgentOS is the
single owner of the persisted Markdown wrapper and YAML frontmatter.

Adding another instance of a supported protocol is a Data Service Source operation. Adding an incompatible protocol still
needs a reviewed Adapter. Web Search candidates resolve source trust from the actual result host when `config.source_levels`
contains a matching domain; otherwise they inherit the channel default.

## Time contract

The Agent derives one integer `lookback_hours` from the objective, defaulting to 48. The deterministic acquisition
Function captures one UTC `collector_cutoff`, invokes all three channel groups with the same plan and derives:

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
- Code owns the query-plan schema, runtime contract, model wiring, adapters, Source Snapshot consumer and Artifact invariants.
- A runtime contract migration may publish a new component version while preserving operator-managed Instructions
  byte-for-byte. Obsolete provider-specific Tool instructions are superseded through code-owned additional context.
- Raw Collection Workflow graph versions remain Studio-managed; Python Function executors remain Git-managed.
- The Workflow exclusively persists the durable Agno session. Studio-managed Collector and Title Curator components
  are rehydrated as database-free runtime copies only when embedded in Workflow Steps, so their outputs remain under
  Workflow step details without competing Agent session writes. Direct Agent runs retain their configured database.

## Invariants

- The Agno Workflow Run ID is the only collection identity.
- Every acquisition group in one run uses the same frozen channel snapshot, cutoff and requested lookback.
- The Web Search group fails closed if the frozen Snapshot exposes more than one enabled Web Search Source.
- The API and RSS groups execute enabled channels in stable priority/code order with bounded concurrency.
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

- Missing provenance, cutoff, invalid query, or invalid lookback produces a stable safe Function error and no write.
- Source Snapshot fetch, authorization, timeout, size or integrity failure aborts the Workflow during acquisition.
- Missing Adapter, provider timeout, invalid response, or request failure is isolated to that channel.
- An acquisition group with no enabled channels returns `no_channels`; sibling groups still execute.
- If no Tool Batch exists after deterministic acquisition, Artifact construction fails rather than publishing false success.
- Build or MinIO failure leaves the final manifest and manifest index unpublished; publication is idempotent for
  immutable matching local files and MinIO objects.
- A matching MinIO object size and SHA-256 metadata is a retry success; a different object under the same key fails.
- Workflow steps use no hidden retries and fail on deterministic Function errors.

## Acceptance seam

Acceptance is observed at the acquisition Function interface and the complete Workflow:

- the complete Data Source Snapshot selects the expected channels once per run;
- Adapter payloads normalize to Candidate contracts;
- concurrent fan-out isolates partial failure;
- all Tool Batches share one exact interval;
- Artifact metadata carries channel and source level;
- Registry exposes no collection acquisition Tools;
- REST, MCP, health and the full validation suite remain green.
