# Tidewise AgentOS Domain Context

## Ubiquitous language

### Investment Conclusion Artifact

One immutable, durable product result emitted after an `investment-reasoning` Workflow run has passed deterministic gates and Reviewer validation. It contains the proposition, final conclusion, layered and node-level assessments, support lineage, risks, limitations, and a context fingerprint. Its identity is the Agno `workflow_run_id`; an identical retry is allowed, while conflicting content must never overwrite it.

### Workflow Run Record

The Agno-owned execution and audit record for one Workflow run. It contains lifecycle state and Step results needed for observability, diagnosis, and retries. It is not the user-facing Investment Conclusion Artifact, even though its final `content` references the same finalized conclusion contract.
