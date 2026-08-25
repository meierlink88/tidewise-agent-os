# Event Extraction Workflow

## Purpose

Event Extraction is a separate Agno PostgreSQL-registered Workflow that consumes complete local Atomic Evidence after
Data Service publication. It groups same-batch Evidence into single-real-world-action Event Candidates and hands each
Candidate to Reasoning Server through an independent authenticated request.

Evidence belongs to one Candidate only when its core actor, real-world action, direct object, event stage, and
occurrence time are the same or compatible. Wording, reporting source, and supplementary details do not change Event
identity. Compound, conflicting, or insufficiently anchored Evidence fails closed to `NEEDS_REVIEW`; non-event content
ends as `NO_EVENT`.

## Runtime boundary

- AgentOS reads resolved Evidence from local published Artifacts. It does not fetch Evidence from Data Service.
- The Event Extractor Agent performs only same-batch semantic grouping and normalization, with no tools or side effects.
- Deterministic Functions claim and freeze a batch, validate a complete one-time partition, persist the draft, submit
  Candidates individually, and journal every reliable acceptance.
- Reasoning Server owns historical recall, final same/new Event resolution, Data Service publication, review state,
  and Graphiti projection.

## Recovery and idempotency

Only Evidence with formal per-item Data identities is eligible. One pending batch is resumed before new Evidence is
selected. Once the Agent result is frozen, publication retries never invoke the Agent again. A stable key derived from
the normalized Event and sorted Evidence IDs lets the publication journal skip already accepted Candidates; Reasoning
Server independently deduplicates an exact replay.

The local batch becomes terminal after every Candidate receives `202 ACCEPTED`; asynchronous Reasoning Server status
is deliberately outside the first Workflow version. Terminal Evidence, including `NO_EVENT` and `NEEDS_REVIEW`, is not
selected again.

## Scheduling

The explicit new-environment seed includes `/workflows/event-extraction/runs` once per minute in `Asia/Shanghai`.
Application startup only inspects Schedule state and never creates, renames, enables, or overwrites it. Existing
environments add the endpoint once through Control Panel before deploying this Workflow.
