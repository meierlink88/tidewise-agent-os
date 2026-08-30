# Event Extraction Workflow

## Purpose and ownership

Event Extraction consumes complete Atomic Evidence and produces formal Data Events, native Graphiti Event Episodes,
and reviewed direct Signal Facts. It has one AgentOS execution path with five business phases:

1. `Extract Events`;
2. `Resolve Events`;
3. `Publish Events`;
4. `Analyze Signals`;
5. `Publish Signals`.

Tidewise semantic judgments belong to three Studio-managed Agno Agents: Event Extractor, Event Identity and Event
Signal Analyst. They receive bounded prepared context, have no Tools and perform no writes. Deterministic Functions in
`capabilities/event` own queue state, leases, retrieval, validation, immutable journals, idempotency and side effects.
`sematica` owns only Graphiti integration primitives and never starts another service.

Every published Workflow version pins the exact published version of all three Agents in Agno component links. The
same mapping is stored in Workflow metadata and therefore in its run records. Seed prompts initialize new Studio
components; execution always uses the published PostgreSQL versions bound to that Workflow version.

## Extract Events

A deterministic Function reads only `data/event/evidence-queue/pending/*.json`, claims at most 20 Evidence by default
and at most 50 when configured, then freezes the exact input under one exclusive lease. The Event Extractor directly
receives that batch and partitions every Evidence ID into either an atomic Event Candidate or `NO_EVENT` disposition.
Malformed Candidate semantics terminate as bounded noncompliant dispositions instead of weakening the formal
contract. Once the draft exists, retries skip the Agent.

## Resolve Events

Candidates are processed one at a time in a bounded loop. Before each Event Identity call, a Function retrieves at
most 30 authoritative historical Event candidates from Data Service and Graphiti native search, freezes that request,
and passes only those identities to the Agent. The Agent judges atomicity and returns exactly one of `NEW_EVENT`,
`SAME_EVENT`, `RELATED_BUT_DISTINCT` or `IGNORED`.

Matched IDs must belong to the frozen history. Deterministic exact-occurrence matches override unsafe model output,
and malformed or invented identity output becomes an `IGNORED` noncompliant disposition for that Candidate. Every
validated decision is immutable before publication begins.

## Publish Events

`SAME_EVENT` and `IGNORED` are terminal and perform no Data, Evidence-link, Episode or Signal write. Only
`NEW_EVENT` and `RELATED_BUT_DISTINCT` use the existing Data Service Event contract and deterministic publication key.
The publication journal independently records:

- Data publication intent;
- the acknowledged formal Data Event and Evidence link;
- successful native Event Episode projection.

Graphiti `add_episode` remains responsible for entity extraction, resolution, `MENTIONS`, ordinary Facts,
deduplication and temporal behavior. Lost acknowledgements resume against deterministic identities without creating a
second Event Episode.

## Analyze Signals

Only Events created and successfully projected by this batch are eligible. Each Event uses two bounded calls to the
same Event Signal Analyst version:

1. classify the Event and produce retrieval hints;
2. preserve that frozen classification and propose direct Signals against supplied existing identities.

Classification is journaled before candidate retrieval, so a retrieval failure cannot cause reclassification. A
Function then retrieves a complete bounded set of eligible Anchors and fundamental Variables and freezes it before the
proposal call. No Agent may invent or alter a UUID. Deterministic validation enforces UUID membership, allowed Anchor
types, Variable direction semantics, time bounds, duplicate pairs and direct Event support. Classification remains in
the Artifact even when the terminal result is `NO_SIGNAL` or `NO_SUPPORTED_ANCHOR`.

## Publish Signals

Each validated proposal has a deterministic key and its own projection acknowledgement. Existing Variable, Anchor and
formal Event Episode identities are revalidated before Graphiti `add_triplet`. Graphiti retains native Fact resolution,
deduplication, temporal contradiction handling and invalidation. A lost acknowledgement may repeat the native call,
but the deterministic Fact identity prevents a second Signal Fact.

After all newly projected Events have terminal Signal outcomes, the Workflow writes the unchanged
`event_extraction_result.v4` manifest and advances the existing pending/processing/completed/failed Evidence queue.
Infrastructure and provider failures release the lease and remain retryable; invalid semantic records terminate safely
per item.

## Model and Graphiti boundary

The only Event paths allowed to use the Agno-to-Graphiti LLM/reranker adapter are Graphiti-native `add_episode`,
`add_triplet`, search and their native resolution internals. Event atomicity, identity, classification and Signal
proposal prompts have no custom `graphiti.clients.llm_client` adapter and live only in published Studio Agents.
Graphiti uses the same AgentOS-registered model and provider; there is no second Event model configuration.

Workflow Functions consume direct predecessor outputs and run-scoped journals. Editable Step display names are never
used as state keys, so Studio renames do not change execution semantics.

## Acceptance checks

A representative validation must prove:

- one successful Workflow run claims Evidence, creates one Data Event, projects one native Event Episode and writes
  only validated direct Signal Facts;
- `SAME_EVENT` leaves Data Event, Evidence link, Episode, `MENTIONS`, ordinary Fact and Signal Fact counts unchanged;
- an Event remains published and classified when no Signal is supported;
- malformed output affects only its bounded Candidate or Event;
- retries after Data, Episode or Signal Fact side effects do not duplicate them;
- changing all Step display names leaves the public result unchanged;
- serialization and strict rehydration retain the exact three Agent version pins;
- run metadata identifies those exact versions.

## Scheduling

The existing new-environment seed keeps `/workflows/event-extraction/runs` once per minute in `Asia/Shanghai`.
Application startup only inspects Schedule state and never creates, renames, enables or overwrites it. This refactor does
not change the endpoint, payload or Data Service API.
