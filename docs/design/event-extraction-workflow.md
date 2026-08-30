# Event Extraction Workflow

## Purpose

Event Extraction consumes complete Atomic Evidence and produces the temporal graph inputs required by investment
reasoning. It has one authoritative execution path with three rename-safe business steps:

1. `extract-events`: claim eligible Evidence and freeze atomic Event Candidates;
2. `publish-events`: resolve historical Event identity, publish new Events to Data Service, and project them with Graphiti
   `add_episode`;
3. `build-signals`: classify newly projected Events, match existing Variables and anchors, and create direct Signal
   Facts.

Each Function consumes Agno's direct predecessor content. Step names are display metadata and may be changed in
Studio without breaking state propagation.

The Workflow runs inside AgentOS. `capabilities/event` owns Event business orchestration while `sematica` supplies
only Graphiti integration primitives; neither starts a second HTTP service or owns a separate worker process.

## Stage 1: extract Event Candidates

AgentOS reads only `data/event/evidence-queue/pending/*.json`; it never scans all historical Evidence manifests.
Each queue item points to one immutable published Evidence Artifact. A deterministic Function claims at most one
batch of 20 Evidence by default, moves its queue items to `processing/<batch-id>/`, resolves their formal content,
and freezes the exact input.
The Event Extractor Agent groups Evidence that represents the same real-world action and returns one strict partition
of:

- atomic Event Candidates;
- `NO_EVENT` Evidence.

Two Events have the same identity only when their actor, real-world action, direct object, event stage, and occurrence
time identify the same occurrence. Wording, reporting source, and supplementary detail do not create a new Event.
Once the Agent result is frozen, retries do not invoke the Agent again.

## Stage 2: resolve, publish, and project

For every frozen Candidate, deterministic code performs bounded historical recall from Data Service and Graphiti,
applies the five-dimensional identity gate, and uses the configured semantic comparator only for plausible matches.

- `SAME_EVENT` is terminal and performs no Data or graph write.
- `IGNORED` is terminal when an atomic Candidate or historical identity cannot be established safely; it performs no
  Data or graph write and completes the Candidate's Evidence as a semantic disposition.
- `NEW_EVENT` and `RELATED_BUT_DISTINCT` publish to Data Service with a deterministic idempotency key.
- A formal Data Event is projected through Graphiti's native `add_episode`, which extracts entities, `MENTIONS`, and
  ordinary Facts.

The publication journal checkpoints intent before the external Data write, the formal Event after publication, and
the Episode after graph projection. A crash therefore resumes the unfinished side effect instead of repeating Event
resolution or creating a second Event.

## Stage 3: construct direct Signal Facts

Only Events newly created and successfully projected by stage 2 enter Signal construction. The Event Analysis
pipeline:

1. classifies the Event domain;
2. retrieves existing graph anchors and Variables without creating ontology entities, applying anchor limits per
   entity type and retaining the complete eligible Variable set inside classification-selected groups;
3. selects only Variable/anchor pairs directly supported by the Event;
4. generates direction, magnitude, impact timing, mechanism, assumptions, invalidation conditions, and confidence;
5. validates every proposal independently and writes accepted `SIGNAL_ON` Facts even when sibling proposals are
   rejected.

Duplicate or semantically ignored Events never run Signal construction. Signal outcomes are journaled per Event so
a retry skips completed analysis.

## Model and Graphiti integration

Graphiti is an in-process SDK. Its native LLM calls use an adapter around the exact model registered in the AgentOS
Registry; no second model configuration or Reasoning Server HTTP call is used. The adapter validates Graphiti's
Pydantic response contract and performs one corrective retry when the provider returns malformed structured output.

The configured embedding provider is also adapted transparently to its maximum batch size. These adapters preserve
Graphiti's native `add_episode`, entity resolution, Fact extraction, deduplication, and temporal invalidation
algorithms; they do not fork or modify Graphiti source code.

## Recovery and idempotency

- Evidence publication creates one idempotent queue item before its own checkpoint advances.
- One processing batch is resumed before new pending Evidence is selected.
- Successful, `NO_EVENT`, duplicate, and semantically ignored Evidence move to `completed`. Infrastructure,
  provider, Data Service, storage, or Graphiti failures release the batch lease for retry and fail the Workflow.
- PostgreSQL stores Agno Workflow runs and schedules, but does not own this business queue state.
- Batch input and Agent output are immutable after freezing.
- A stable Candidate key is derived from the normalized Event and sorted Evidence IDs.
- Data publication uses a stable submission identity.
- Publication and Signal journals checkpoint each irreversible side effect.
- A batch becomes terminal only after every Candidate reaches a safe disposition and every newly projected Event has
  a terminal Signal result.

## Acceptance checks

A representative real-Evidence validation must demonstrate:

- an atomic Event is extracted and published once;
- one Event Episode is created with `episode_kind=EVENT` and its Data Event identity;
- Graphiti creates ordinary Facts and `MENTIONS` from the Event;
- supported Variable/anchor pairs create validated Signal Facts;
- replaying the exact same Event Candidate returns `SAME_EVENT` and leaves Episode, `MENTIONS`, ordinary Fact, and
  Signal Fact counts unchanged;
- changing any of the three Step display names leaves the complete Workflow result unchanged;
- a crash after publication intent, Data publication, or an Episode write with a lost acknowledgement resumes the
  remaining work without creating a second Event, `MENTIONS`, ordinary Fact, or Signal Fact;
- failed publication and Signal stages expose a safe stage name and diagnostic ID while logs retain only exception
  types and code-frame locations, never provider payloads or secrets.

## Scheduling

The explicit new-environment seed includes `/workflows/event-extraction/runs` once per minute in `Asia/Shanghai`.
Application startup only inspects Schedule state and never creates, renames, enables, or overwrites it. Existing
environments add the endpoint once through Control Panel before deploying this Workflow.
