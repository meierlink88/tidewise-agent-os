# Raw Collection Workflow — first vertical slice

## Outcome

Deliver one real, inspectable collection run through AgentOS:

1. a Collector Agent interprets a collection objective and calls a channel tool;
2. the tool writes the complete direct response candidates into a run-scoped buffer;
3. deterministic code builds an Artifact set;
4. publication writes the manifest last;
5. the Agno Workflow Run completes only after publication succeeds.

The first slice uses Eastmoney stock-news search. The other six channels and the
Scheduler are deliberately deferred until this seam is proven over REST and MCP.

## Non-goals

- Recreate AgentRun definitions, versions, executions, invocations, or HTTP API.
- Let the model rewrite source content into the canonical Artifact.
- Add a Team, conversational memory, knowledge, or autonomous long-term state.
- Enable a recurring schedule before tool budgets and cross-run deduplication are proven.

## Runtime shape

```text
CollectionRequest
  -> agentic-collect       (Collector Agent + channel tools)
  -> build-artifact-set    (deterministic Python)
  -> publish-collection    (deterministic, manifest last)
  -> CollectionResult
```

The seven channels are Agent tools, not Workflow steps. The Agent decides how to
query and when to stop. Workflow steps define the reliable delivery process.

## Interfaces

### CollectionRequest

- `objective`: non-empty natural-language collection objective.
- Temporal requirements live inside `objective`; there is no second, potentially
  conflicting Workflow time field.

### Prompt configuration

- The Collector and the Raw Collection Workflow are Agno Studio components stored
  in the AgentOS database, not code-registered runtime components.
- `agents/raw_collector.seed.md` seeds Agent published version 1 only when the component does
  not exist. Subsequent prompt edits belong to Agno Studio versions in PostgreSQL;
  the seed never overwrites operator changes.
- `capabilities/raw_collection/functions/` owns the three Python Function executors;
  `workflows/raw_collection.py` only composes them and seeds Workflow published
  version 1 when the component does not exist.
  Subsequent graph edits belong to Agno Studio versions in PostgreSQL.
- The Workflow loads the component's current published version from PostgreSQL at
  the start of every run. Publishing or rolling back a Studio version therefore
  affects the next collection run without a process or container restart.
- The code Registry owns the allowed model, database, and channel tools required
  to rehydrate the Studio component.
- A missing, draft-only, or un-rehydratable Collector component fails before a
  model or Tool call.
- Code-owned runtime contract migrations may publish a new component version,
  but must preserve the current operator-managed instructions byte-for-byte.

### Channel tool receipt

Tools return a small receipt to the model. Complete candidates are written to the
run-scoped Collection Buffer and never depend on the model reproducing them.

- `batch_id`
- `connector`
- `query`
- `requested_after`
- `requested_before`
- `result_count`
- `in_window_result_count`
- `candidate_ids`

### PreparedArtifactSet

- workflow run identity;
- pending publication directory;
- candidate terminal counts;
- accepted document paths and hashes;
- publication outcome: `changed` or `no_change`.

### CollectionResult

- Agno Workflow Run identity;
- outcome;
- accepted count;
- final manifest path.

## Invariants

- The Agno Workflow Run ID is the only run identity.
- Every Tool candidate preserves connector, query, URL, source, collection time,
  publication-time hint, title, and direct content.
- The Agent may choose queries but cannot mutate the complete buffered candidates.
- The Agent must translate the objective's temporal language into an explicit,
  timezone-aware `published_after` / `published_before` interval on every Tool call.
- Every Tool validates its requested interval and persists it with the complete
  direct-response batch. A connector should push the interval upstream when its
  provider supports that capability; otherwise Artifact construction enforces it.
- Every candidate reaches exactly one terminal disposition before publication.
- `results_pending` is zero before publication.
- Accepted documents, candidate ledger, summary, and index publish before the manifest.
- `manifest.json` is the completed Artifact marker and is published last.
- Keys, authorization headers, and raw provider errors never enter Artifacts.
- Every Tool Batch and manifest identify the exact Collector component version
  and instruction hash used for the run.
- Published Artifacts live under the Git-ignored project directory
  `data/collector/` by default; Compose binds it to `/app/data/collector`.

## Failure semantics

- Input validation is the first operation inside `agentic-collect` and occurs before any model or Tool call.
- A missing or invalid published Studio Agent fails closed before model execution.
- A missing, naive, reversed, excessive, or implausibly future Tool interval returns
  a safe validation error and writes no batch.
- An individual channel tool returns a safe error to the Agent and may be followed by
  another tool call.
- No buffered candidates makes Artifact construction fail.
- Build failure leaves the final Artifact tree unchanged.
- Publish is idempotent for already-published immutable files with matching hashes.
- All Workflow steps explicitly use `max_retries=0` and `on_error="fail"`.

## Acceptance seam

The first slice is accepted when a real AgentOS Workflow REST run:

- produces a DeepSeek tool call to Eastmoney;
- writes a run-scoped Tool Batch;
- publishes at least the candidate ledger, summary, dedup index, and manifest;
- returns a non-empty `CollectionResult`;
- exposes the Agent, Workflow, and Tool activity in AgentOS;
- leaves the MCP check green.
