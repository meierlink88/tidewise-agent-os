# Event batch item isolation

Issue: #207. Scope: local Event extraction, identity, association and Signal processing.

## Behavior

- Agent Steps retain their JSON output schema, but defer parsing to the following Function.
- Functions parse response rows independently and ignore extra fields. They do not reclassify
  or correct LLM conclusions. Malformed rows, unusable routing keys and invalid references
  are recorded and the affected Event is skipped rather than failing its siblings.
- `REGIONAL` is not automatically mapped to one of the four Event classes. That candidate
  is recorded as rejected and does not proceed to identity, association or publication.
- Missing matches remain a normal no-publication outcome. Missing required response rows
  are failures, not successful empty matches.
- Journal `failure_reason` distinguishes failures from ordinary ignored Events. Completion
  reports failed candidates and Evidence IDs. Existing publication receipts are retained,
  including partial writes; this is not a distributed rollback mechanism.
- Raw model output is checkpointed before parsing and reused during recovery. Infrastructure
  failures and corrupt durable state can still stop a run; they must not be hidden as success.
- Native Agent Steps, Parallel branches and Loops are unchanged. No new test cases were added
  while the separately requested test removal PR is pending.

## Local acceptance, 2026-09-07 Asia/Shanghai

The user authorized loading the development branch locally before PR merge.

Real run: `1bc6e429-40f3-4932-bffb-2831f5d779f1`.
Session: `7810ad88-30e8-4a1b-ac6b-c5e57b8ded04`.

- Workflow terminal status: COMPLETED; 20 Evidence and 20 Event candidates.
- 13 published Events were read back through the Data Service list API.
- All 13 Neo4j Event projections have exactly the selected association UUID sets.
- Two Signal relationships were read back with source Event IDs and variable/anchor endpoints:
  India solar installations → effective capacity; Chinese humanoid shipments → market demand.
- Two Events failed locally and had no publication receipt: an out-of-catalog association,
  and an unusable identity response. Five further Events had no publishable match.
- Historical REGIONAL response replay preserved 18 valid candidates and rejected one.
- Final-code replay injected REGIONAL into a completed 20-candidate response: 19 accepted,
  one rejected. Raw-checkpoint recovery parsed that response without reading/calling an Agent.
- Ruff, mypy, HTTP health and MCP smoke verification passed.

This proves batch completion and data integrity for this run, not universal semantic accuracy.
No schedules were enabled, no historical data was deleted, and no PR was merged by the agent.

## Rollback

Restore the prior code revision and reload the local service. Preserve journals, raw responses,
receipts and published data. Do not delete a pending batch or reverse database writes as part
of a code rollback without separate operator authorization.
