# Tidewise AgentOS Domain Context

## Ubiquitous language

### Atomic Evidence

The smallest complete, source-grounded business proposition from which an Event may be formed. It distinguishes the
business actor and action from the source that reports or claims them, and may carry explicit reason, method and
quantitative observations needed to understand the proposition.

### Evidence Attribution

The provenance describing who reported or claimed an Atomic Evidence proposition. Attribution belongs to Evidence
and the relationship connecting Evidence to an Event; it is not an Event participant or part of a merged Event's
meaning.

### Event

One formalized real-world business occurrence, announcement, plan or expectation assembled from compatible Atomic
Evidence. Its meaning includes actors, action, objects, lifecycle stage, modality, Event Time, jurisdictions, and any
explicitly supported reason, method and quantitative observations. An Event does not inherit source attribution.

### Event Time

The Event-owned temporal meaning that distinguishes when something occurred, was announced or becomes effective,
together with the precision of that knowledge. At least one of those temporal perspectives must be known, even when
its precision is explicitly unknown.

### Event Identity

The rule for deciding whether two Event descriptions denote the same real-world Event. It compares exactly five
dimensions: actors, action, objects, lifecycle stage and compatible Event Time. Reason, method, quantitative
observations, modality and jurisdiction enrich meaning but do not independently turn the descriptions into the same
or a different Event.

### Evidence Metric

A canonical quantitative observation supported by Evidence. It names the measure and retains the stated value or
change plus any unit and period. Repeated equivalent observations may be merged deterministically, but their supported
business meaning must not be discarded.

### Investment Conclusion Artifact

One immutable, durable product result emitted after an `investment-reasoning` Workflow run has passed deterministic gates and Reviewer validation. It contains the proposition, final conclusion, layered and node-level assessments, support lineage, risks, limitations, and a context fingerprint. Its identity is the Agno `workflow_run_id`; an identical retry is allowed, while conflicting content must never overwrite it.

### Workflow Run Record

The Agno-owned execution and audit record for one Workflow run. It contains lifecycle state and Step results needed for observability, diagnosis, and retries. It is not the user-facing Investment Conclusion Artifact, even though its final `content` references the same finalized conclusion contract.
