# Event Extraction Workflow

## Purpose and ownership

Event Extraction consumes complete Atomic Evidence and produces formal Data Events, native Graphiti Event Episodes,
and reviewed direct Signal Facts. Studio exposes exactly one bounded outer Agno `Loop`. That visible `Loop` directly
contains exactly five business Function Steps, in this order:

1. `Extract Events`;
2. `Resolve Events`;
3. `Publish Events`;
4. `Analyze Signals`;
5. `Publish Signals`.

The canvas contains no visible `Condition`, nested `Condition`, nested `Loop`, `Steps`, `Router` or branch node. Each
Function Step encapsulates its own per-Candidate or per-Event iteration, conditional decisions, prepare/freeze logic,
retry and recovery decisions, skip rules and terminal handling. The outer `Loop` remains visible and bounded so the
published Studio shape expresses the five-phase business lifecycle without exposing recovery machinery as workflow
topology.

Tidewise semantic judgments belong to three Studio-managed Agno Agents: Event Extractor, Event Identity and Event
Signal Analyst. They receive bounded prepared context, have no Tools and perform no writes. Deterministic Functions in
`capabilities/event` own queue state, leases, retrieval, validation, immutable journals, idempotency and side effects.
`sematica` owns only Graphiti integration primitives and never starts another service.

Every published Workflow version pins the exact published version of all three Agents in explicit Agno component
links. The same mapping is stored in Workflow metadata. Before every Agent invocation, the Function immutably binds a
stable semantic operation key to the exact Agent ID and version in the batch's `agent-executions.json` recovery
journal, then mirrors every invoked binding in run metadata. Recovery reuses an identical binding and fails closed on
version drift. Execution never resolves `current`/`latest` implicitly. Recovery Artifacts already using this Event
wire but lacking the new audit journal remain readable without fabricating historical version data.

Agno derives ordinary `Workflow.save()` links only from Agent/Team/Workflow executors visible on a Step. These five
Steps must remain pure Function Steps, so Event Workflow versions are code-managed and may be published only through
the repository's exact-link publisher. The Workflow remains visible in Studio, but generic Studio/SDK republication is
unsupported; its publication-policy metadata and startup validation fail closed rather than execute a version whose
links were dropped. The three Agents, in contrast, remain independent Studio-managed components with editable
prompts. Editable Step display names are presentation only and are never state-lookup keys.

## Event wire contract

The Event business object has exactly three top-level fields: `title`, `summary` and `semantic`. `semantic` has exactly
these fields:

- `actors`;
- `action`;
- `objects`;
- `stage`;
- `modality`;
- `time`;
- `jurisdictions`;
- `reason`;
- `method`;
- `metrics`.

`semantic.time` has exactly `occurred_at`, `announced_at`, `effective_at` and `precision`. At least one of the three
timestamps must be present and every present timestamp is UTC. `precision` is one of `INSTANT`, `DAY`, `RANGE`,
`MONTH`, `QUARTER`, `YEAR` or `UNKNOWN`. There is no top-level `modality`, `occurred_at` or `announced_at`; semantic
content has one owner.

`reason` and `method` retain only content explicitly supported by compatible Evidence. `metrics` uses the complete
EvidenceMetric proposition (`name`, `value`, `unit`, `change`, `period`) and is deterministically deduplicated without
silently dropping supported quantitative context. The pinned Extractor owns the semantic compatibility judgment; the
deterministic boundary admits only a selected reason or method that occurs verbatim in at least one supporting
Evidence, recovers a sole supported value, and otherwise preserves the Agent's null conflict disposition. Source
attribution is an Evidence and Event-Evidence Link concern; it is never copied into the merged Event. The Data
publication request carries this exact business object. Its acknowledgement returns the formal Event ID and the
required Data-owned `status`.

## Extract Events

A deterministic Function reads only `data/event/evidence-queue/pending/*.json`, claims at most 20 Evidence by default
and at most 50 when configured, then freezes the exact input under one exclusive lease. The exact pinned Event
Extractor version receives that batch and partitions every Evidence ID into either an atomic Event Candidate or
`NO_EVENT` disposition. Its contract preserves supported `reason`, `method` and `metrics` from the complete Evidence
business proposition, while excluding Evidence attribution. Malformed Candidate semantics terminate as bounded
noncompliant dispositions instead of weakening the formal contract. Once the draft exists, retries skip the Agent.

## Resolve Events

Candidates are processed one at a time in a bounded loop inside the Function Step. Before each Event Identity call, a
Function retrieves at most 30 historical Event candidates exclusively from both Graphiti Episode full-text recall and
Graphiti Anchor/`MENTIONS` recall, freezes that request, and passes only those identities to the exact pinned Agent.
Both Graphiti recall paths are required and fail closed: an unavailable or malformed recall cannot be treated as an
empty history. Malformed matched Event content fails closed when it would otherwise be the entire recalled set, while
one malformed item is isolated when valid formal Event history remains. Historical resolution performs no Data
Service GET, pagination, local date-window filtering or Data authority/fallback. The Agent judges atomicity and
returns exactly one of `NEW_EVENT`, `SAME_EVENT`, `RELATED_BUT_DISTINCT` or `IGNORED`.

Matched IDs must belong to the frozen history. Deterministic exact-occurrence matches override unsafe model output,
and malformed or invented identity output becomes an `IGNORED` noncompliant disposition for that Candidate. Every
validated decision is immutable before publication begins. Identity remains exactly five-dimensional: `actors`,
`action`, `objects`, `stage` and compatible Event time. `reason`, `method`, `metrics`, `modality` and `jurisdictions`
provide Event meaning but do not expand the `SAME_EVENT` gate.

## Publish Events

`SAME_EVENT` and `IGNORED` are terminal and perform no Data, Evidence-link, Episode or Signal write. Only
`NEW_EVENT` and `RELATED_BUT_DISTINCT` POST the frozen Event wire contract to Data Service with a deterministic
publication key. Data Service is used only for new formal Event and Event-Evidence Link publication and the returned
formal Event ID; it is not a historical Event source. The publication journal independently records:

- Data publication intent;
- the acknowledged formal Data Event and Evidence link;
- successful native Event Episode projection.

Graphiti `add_episode` remains responsible for entity extraction, resolution, `MENTIONS`, ordinary Facts,
deduplication and temporal behavior. Lost acknowledgements resume against deterministic identities without creating a
second Event Episode.

## Analyze Signals

Only Events created and successfully projected by this batch are eligible. Each Event uses two bounded calls to the
same exact pinned Event Signal Analyst version. The Analyst receives the complete Event semantic, including supported
`reason`, `method` and `metrics`, but no Evidence attribution:

1. classify the Event and produce retrieval hints;
2. preserve that frozen classification and propose direct Signals against supplied existing identities.

Classification is journaled before candidate retrieval, so a retrieval failure cannot cause reclassification. A
Function then retrieves a complete bounded set of eligible Anchors and fundamental Variables and freezes it before the
proposal call. `IndustryChain` may be used as retrieval context to expand its canonical members, but it is never
exposed as a direct Signal Anchor; industrial Signals terminate on an existing `ChainNode`. No Agent may invent or
alter a UUID. Deterministic validation enforces UUID membership, allowed Anchor types, the `ChainNode` ownership rule,
Variable direction semantics, time bounds, duplicate pairs and direct Event support. Classification remains in the
Artifact even when the terminal result is `NO_SIGNAL` or `NO_SUPPORTED_ANCHOR`.

## Publish Signals

Each validated proposal has a deterministic key and its own projection acknowledgement. Existing Variable, Anchor and
formal Event Episode identities are revalidated before Graphiti `add_triplet`; the writer rejects `IndustryChain`
targets even if stale catalog or model output reaches this boundary. Graphiti retains native Fact resolution,
deduplication, temporal contradiction handling and invalidation. A lost acknowledgement may repeat the native call,
but the deterministic Fact identity prevents a second Signal Fact.

After all newly projected Events have terminal Signal outcomes, the Workflow writes the unchanged
`event_extraction_result.v4` manifest and advances the existing pending/processing/completed/failed Evidence queue.
Infrastructure and provider failures release the lease and remain retryable; invalid semantic records terminate safely
per item.

The v8 recovery topologies remain supported: missing resolution journals can be reconstructed from existing
publication checkpoints, failed-publication outcomes remain terminal and repeatable, legacy terminal Signal journals
skip re-analysis, and lease takeover remains fenced. The frozen wire contract deliberately has no legacy decoder.
Consequently, a pending batch serialized with the obsolete top-level time/modality Event shape cannot also satisfy the
new strict wire contract. Deployment must prove that no such old-wire pending batch remains; this is the explicit
resolution of the otherwise incompatible requirements to preserve v8 recovery while forbidding an old-wire
compatibility branch.

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
- serialization and strict rehydration preserve one visible bounded outer `Loop` with exactly the five direct
  Function Steps and no visible condition or branch topology;
- serialization and strict rehydration retain the exact three Agent version links;
- generic Studio/SDK Workflow republication is unsupported, while the code-managed publication policy rejects a
  current version that lacks its exact links;
- runtime never loads an implicit Agent version, and both the durable per-operation batch journal and run metadata
  identify every exact invoked version;
- history resolution performs no Data Service GET while new Event publication still POSTs to Data Service;
- failure of either required Graphiti recall path fails closed;
- the Event request, Data acknowledgement, Graphiti Episode content and Signal Analyst input preserve the exact Event
  semantic contract without attribution or legacy top-level time/modality fields.

## Scheduling

The existing new-environment seed keeps `/workflows/event-extraction/runs` once per minute in `Asia/Shanghai`.
Application startup only inspects Schedule state and never creates, renames, enables or overwrites it. This refactor does
not change the Schedule, Workflow endpoint or Workflow payload. The Data publication endpoint remains unchanged; its
Event wire contract migration is governed by the deployment coordination below.

## Deployment coordination

This AgentOS change must not be deployed, and existing Event data must not be cleared, from this repository change.
Deployment is allowed only after the independent Data provider contract Pull Request has been merged and the owners
have coordinated clearing Events written with the obsolete contract. Before upgrade, operators must also verify that
`data/event/.pending` contains no batch serialized with the obsolete Event wire; such batches must be completed on the
old runtime or handled by an explicitly coordinated migration decision outside this no-compatibility change. The
coordinated clear and pending-batch audit are explicit migration gates, not AgentOS startup or fallback behavior.
