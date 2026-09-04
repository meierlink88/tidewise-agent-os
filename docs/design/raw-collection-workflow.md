# Raw Collection Workflow

## Outcome

Raw Collection has three business-visible Steps and one semantic filtering Agent:

```text
CollectionRequest
  -> collect-raw-evidence
       -> load one immutable Source Snapshot
       -> execute Web Search, API and RSS groups concurrently
       -> persist complete Tool Batches
       -> expose bounded Candidate context
  -> filter-raw-evidence
       -> Raw Evidence Filter returns bounded partial is_relevant decisions
       -> persist valid decisions and requeue omissions into the next batch
  -> publish-raw-evidence
       -> validate exact Candidate coverage
       -> deduplicate and build immutable Raw Documents
       -> publish MinIO objects, local index and manifest
  -> CollectionResult
```

The Schedule message is the collection query. There is no query-planning Agent or separate query-plan contract.
The filter receives `candidate_id`, title, source, publication-time hint and a bounded content excerpt. It removes
clearly non-political-economic material while conservatively retaining ambiguous material for Evidence Extraction.

## Source Snapshot and acquisition

Data Service is the only Source management authority. `collect-raw-evidence` reads the complete active Snapshot exactly
once per run and freezes it inside the Function execution context. Web Search allows at most one enabled Source; API and
RSS execute all enabled Sources in stable priority/code order with bounded concurrency.

Each channel returns at most its configured `max_results`, normally the latest ten results. Bocha always requests
`freshness=oneDay`. Raw Collection does not calculate a shared cutoff, infer lookback hours or reject Candidates by
publication time. `published_at` remains source metadata for downstream analysis.

Provider results normalize to Candidate values containing channel code, query, URL, source identity, trust level,
content, publication-time hint and collection time. Provider failures are isolated per channel and sanitized before
receipts or Artifacts are written.

## Adapter seam

Data Service owns Source instances and operational settings; Python owns incompatible provider protocols. Supported
Adapter keys are:

- Web Search: `bocha`, `tavily`, `parallel`;
- structured API: `cls`, `eastmoney_fast`, `eastmoney_stock`, `stcn`;
- dynamic feed: `generic_rss`.

The same Schedule query is used by query-capable providers. Fixed latest-feed providers may ignore it. Tavily requests
plain text; Bocha, Parallel, structured APIs and RSS normalize provider summaries, snippets or feed content. AgentOS is
the only owner of the persisted Markdown wrapper and frontmatter.

## Runtime ownership

- Raw Evidence Filter is a published Agno Studio Agent component in PostgreSQL.
- Its prompt, strict output schema and runtime wiring are contract-versioned in Git.
- Raw Collection Workflow graph versions remain Studio-managed; Function implementations remain Git-managed.
- Acquisition functions are not Agent Tools and cannot be selected for autonomous tool calling.
- The Workflow Run ID is the only collection identity.

## Invariants

- Every successful channel call writes one complete Tool Batch.
- Every Candidate receives exactly one strict boolean relevance decision before publication. Valid decisions from a
  partial model response are retained; omitted Candidate IDs are retried in later bounded batches.
- Invalid URLs, irrelevant material and cross-run or in-run duplicates receive deterministic terminal dispositions.
- No publication-time window participates in Candidate acceptance.
- Manifest publication is last and downstream Evidence Extraction consumes the manifest index by byte offset.
- Every accepted Markdown document is uploaded immutably to the configured MinIO `raw-evidence` bucket before its
  manifest becomes visible.
- Provider keys, raw errors and unbounded bodies never enter Agent input, logs or published audit ledgers.

## Failure semantics

- Invalid query or Source Snapshot failure aborts collection before publication.
- One provider failure does not discard successful sibling channel results.
- A group without enabled Sources returns `no_channels`; sibling groups still execute.
- Unknown or duplicate filter IDs abort immediately. An omitted ID is retried up to three model responses; a third
  omission aborts with the exact exhausted IDs. Final publication still requires complete Candidate coverage.
- Build or MinIO failure leaves the final manifest and manifest index unpublished.
- All three Workflow Steps have no framework retries. The filter Loop owns its explicit, persisted omission retry.

## Acceptance seam

- Studio shows exactly `collect-raw-evidence`, `filter-raw-evidence` and `publish-raw-evidence`.
- No Collection Query Planner is registered or executed.
- Bocha sends `freshness=oneDay`.
- Latest provider results are not rejected by a local time-window check.
- Filter input is bounded; partial valid output advances progress without losing omitted Candidates, and publication
  still covers every Candidate exactly once.
- Artifact publication remains deterministic and idempotent.
