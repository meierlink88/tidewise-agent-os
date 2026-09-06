# Event Extraction Workflow

## v15 ownership and topology

Issue #171 simplifies the #165 four-Agent workflow to one batch per invocation and one Event Loop.
Agents recommend structured meanings only. Functions own retrieval, IDs, validation,
state transitions, leases, journals, pagination, retries and all external writes.

```text
Claim one bounded Evidence batch
Event Extractor
Event Loop
  Prepare candidate and frozen historical candidates
  Event Identity + Classification
  Load class-specific catalog
  Event Association
  Prepare direct Signal candidates
  Event Signal Analyst
  Validate, publish and complete Event
Complete batch and return
```

Only the Event Loop is visible/configurable in Studio. There are no Condition nodes,
outer batch loops or editable pagination loops. The scheduler owns the next invocation.
Functions decide empty, duplicate, non-publishable and checkpoint skip states. The narrow
`app/event_step_runtime.py` adapter rebinds ordinary native Agent Steps after hydration,
applies those gates and invokes only the current Step's exact pinned Agent. It validates
and freezes each semantic result through Functions before moving on. It supports sync,
async and both streaming modes; skipping never asks an LLM to decide or sets `stop=True`
inside the Event Loop. An empty candidate list performs one inert iteration then completes.
An empty/busy batch terminates at the top-level claim Step without any Agent invocation.
All Steps fail closed on errors. Display names are not state keys. No Function invokes an Agent.
The registered v13 Functions remain available only for historical Workflow rehydration;
the frozen v13 topology fixture tests those legacy contracts separately.

## Agent contracts

### Model input transport

Issue #173 fixes Agno 3.0.1 interpreting business dictionaries as Message envelopes.
All four direct Event Agent Steps now receive explicit JSON text as user content.
Both predecessor representations are replaced; nested stale outputs cannot override
the payload. Unicode, nulls and numeric values are retained. Association/Signal inputs
must contain Event, frozen classification and identified nonempty candidates; invalid
or unserializable input fails before model invocation. Regression tests pass through
native Step message selection and Agno's real sync/async message builders, rather than
mocking Agent.arun and bypassing serialization. No new Workflow nodes are introduced.

| Agent | Semantic output | Code-owned boundary |
| --- | --- | --- |
| Event Extractor | Atomic Event candidates or explicit no-event dispositions | Exact Evidence partition, source fields, timestamps |
| Event Identity | Identity decision and one primary class | Frozen historical ID membership and exact occurrence checks |
| Event Association | Matching supplied UUIDs and reasons, or explicit no-match reason | Complete catalog coverage, type and page membership |
| Event Signal Analyst | Direct relative-time Signal proposals or no-signal reason | Endpoint membership, allowed types, direction and temporal bounds |

Primary classes are GEOPOLITICAL, MACRO_ECONOMIC, INDUSTRY_CHAIN and COMPANY.
ChainNode is in the industrial class, not a fifth class.
Malformed outputs are failures, not invented success or automatic no-match dispositions.
A deterministic gate cannot prove semantic truth; directness still requires an evidence-bounded
model judgment and evaluation. It can and does reject invalid identities, types and timing.

Association and direct-Signal Skills contain methods and examples only. They are rebound on
Agent hydration and before nested Workflow execution because Agno storage omits Skills.
Skills may read their local guidance, but have no business tools or write responsibilities.

## Catalog coverage and entity types

| Primary class | Association catalog |
| --- | --- |
| GEOPOLITICAL | All authoritative GeopoliticRivalry profiles |
| MACRO_ECONOMIC | All authoritative MacroEconomic profiles |
| INDUSTRY_CHAIN | All IndustryChain profiles, then all canonical members of selected chains |
| COMPANY | Exact name/alias candidates from explicit Event actors and objects; model disambiguates |

Large catalogs are fully partitioned into internal technical pages, never cut to Top-K.
The direct Agent Step adapter processes these pages with per-page journal checkpoints;
pages are not additional configurable workflow steps or independent business loops.
IndustryChain selection still precedes loading its canonical ChainNode members.
Each association page contains at most 64 profiles and 48,000 serialized characters;
an oversized profile or overall safety-cap overflow fails explicitly.
Only approved matching fields enter profiles. Candidate assets, main transmission,
uncontrolled summaries and unrelated node data are excluded. Core propositions and tactics
are contextual profile information, never evidence of downstream effects.

Company resolution is intentionally exact-name/alias bounded, not a full scan of every
company by the model. An unresolved company remains a no-match; recall limitations are not
filled with invented IDs. No new entity is created.

Signal candidates include selected eligible anchors and additional cross-layer exact
name/alias matches to Event actors/objects. IndustryChain never becomes a Signal anchor.
All eligible FUNDAMENTAL Variables are covered in pages of at most 64 variables by 30 anchors.
A candidate's availability is not evidence of a variable change. The Analyst cannot infer
an unstated intermediate action or downstream investment outcome.

## Controlled graph publication

New Event publication does **not** call Graphiti `add_episode` or pass the complete
ontology `ENTITY_TYPES` for free extraction. Code atomically writes a native Episodic
Event and MENTIONS only to the selected existing entities. The whitelist is
GeopoliticRivalry, MacroEconomic, IndustryChain, ChainNode and Company, further narrowed
by the primary class and selected chain membership. Each endpoint is revalidated by
UUID, group, label and authoritative business ID at write time.

No generic entities, ordinary relationship Facts or unrelated entity types are created
by this path. Replays require identical Event content and association digest.
Missing or conflicting endpoints fail closed. This is stricter than merely narrowing an
LLM entity-type registry and prevents publication from rerunning semantic association.

Only after the Data ACK and graph ACK may existing Graphiti Signal `add_triplet`
integration execute. Its native resolution and deterministic Signal identity are retained.
The Data API and Event-Evidence publication contract are unchanged. SAME_EVENT and IGNORED
remain terminal with no Data, Evidence-link, Episode or Signal writes.

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


## Versioning, recovery and deployment

Published workflows carry four exact native step-agent links and matching metadata.
Links are derived using Agno's native `derive_step_links`, so Studio serialization and
code publication agree. Startup rejects missing/mismatched pins, not editable positions.
Refreshing Agent versions preserves the published Event Loop configuration. Contract
v14 to v15 migration seeds the approved linear topology and preserves the four exact pins.
The semantic page boundaries are unchanged. Journals created after #173 carry
`input_transport_version=2`. Missing or older transport versions fail closed and require
explicit operator reconciliation, even with unchanged Agent pins: earlier cached empty
association decisions may have been produced without any business input. Never merely
stamp an old journal with version 2; review/reset contaminated semantic results while
preserving every valid Data/graph publication identity and ACK under a separate approved
reconciliation. This code change does not mutate or replay existing pending batches.

Each batch freezes pins in `storyline_journal.json`. Inputs, catalog pages, decisions,
compiled proposals and per-write acknowledgements are journaled under the existing lease.
Recovery skips frozen semantic decisions and resumes only unfinished deterministic writes.
Data publication keys, Episode UUIDs and Signal identities are deterministic.
A different Agent-version set cannot take over a partially analyzed batch.

Old pending drafts without the v14 journal fail closed. Operators must finish them under
the original Workflow or explicitly reconcile them before upgrading. No automatic pending
deletion, history deletion, graph reset, database migration or Schedule enablement occurs.
Agent failures before a Function regains control leave the bounded lease to expire;
Function failures release only their own lease for prompt recovery.

After human PR merge, audit pending batches, update the local runtime, verify published
Agent contracts and four links, then run authorized REST/MCP and real-data acceptance.
No provider API migration is needed. Roll back to the previous image/config for old batches;
do not replay a storyline journal through a pre-v14 workflow.

## Acceptance

Tests cover real nested Agno execution and storage roundtrip; renamed steps; full catalog
coverage; invalid IDs and primary types; empty outcomes; duplicate no-op; partial write
recovery without repeated semantic calls; exact pins; legacy pending rejection; projection
whitelists; and the absence of free Graphiti Event extraction.
Live semantic quality and external-service acceptance are separate from mocked I/O tests.

Framework references: [Agno Steps](https://docs.agno.com/workflows/workflow-patterns/step-based-workflow)
and [Agno Skills](https://docs.agno.com/skills/overview). Runtime behavior was checked against
the installed Agno 3.0.1 source, including native step-link derivation and nested hydration.
