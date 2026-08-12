# Raw Collection Workflow

## Outcome

Raw Collection uses an Agent for query planning and three stable Tool façades for acquisition, then deterministic
Workflow Functions build and publish immutable Artifacts:

```text
CollectionRequest
  -> agentic-collect
       -> web_fetch  -> one enabled Web Search channel
       -> api_fetch  -> all enabled API channels, bounded concurrent
       -> rss_fetch  -> all enabled RSS/Atom channels, bounded concurrent
  -> build-artifact-set
  -> publish-collection
  -> CollectionResult
```

Complete provider results go directly to the run-scoped Collection Buffer. The model receives only compact receipts
and never reconstructs source content.

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

Adding another instance of a supported protocol is a database operation. Adding an incompatible protocol still needs a
reviewed Adapter. Web Search candidates resolve source trust from the actual result host when `config.source_levels`
contains a matching domain; otherwise they inherit the channel default.

## Time contract

The Agent derives one integer `lookback_hours` from the objective, defaulting to 48. The Workflow captures one UTC
`collector_cutoff` before model execution. All three Tools internally derive:

```text
published_before = collector_cutoff
published_after  = collector_cutoff - lookback_hours
```

There is no time-window Tool and the Agent never supplies absolute interval strings. Providers receive the interval when
their protocol supports it; deterministic Artifact construction always enforces it.

## Prompt and runtime ownership

- Collector business instructions remain a published Agno Studio component in PostgreSQL.
- Code owns the Tool list, runtime contract, model wiring, Tool budget, adapters, catalog and Artifact invariants.
- A runtime contract migration may publish a new component version while preserving operator-managed Instructions
  byte-for-byte. Obsolete provider-specific Tool instructions are superseded through code-owned additional context.
- Raw Collection Workflow graph versions remain Studio-managed; Python Function executors remain Git-managed.

## Invariants

- The Agno Workflow Run ID is the only collection identity.
- Every Tool in one run uses the same cutoff and requested lookback.
- `web_fetch` fails closed if the catalog exposes more than one enabled Web Search row.
- `api_fetch` and `rss_fetch` execute enabled channels in stable priority/code order with bounded concurrency.
- One provider failure does not discard successful sibling results; raw provider errors and keys never enter receipts or
  Artifacts.
- Every Candidate preserves channel code, query, URL, original publisher, effective source level, direct content,
  publication-time hint and collection time.
- Every successful channel call writes a complete Tool Batch before returning its receipt.
- Every Candidate reaches exactly one deterministic terminal disposition before publication.
- Manifest publication is last; downstream Workflows consume `indexes/manifest-index.jsonl` by byte offset.
- Published files remain under the Git-ignored `data/collector/` root.

## Failure semantics

- Missing provenance, cutoff, invalid query, or invalid lookback produces a stable safe Tool error and no write.
- Missing Adapter, provider timeout, invalid response, or request failure is isolated to that channel.
- A façade with no enabled channels returns `no_channels` so the Agent can continue the remaining façades.
- If no Tool Batch exists after Agent execution, Artifact construction fails rather than publishing false success.
- Build failure leaves the final tree unchanged; publication is idempotent for immutable matching files.
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
