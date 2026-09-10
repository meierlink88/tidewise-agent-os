# Raw Collection DeepSeek local timing

Issue #235; measured 2026-09-10 on local AgentOS running Agno 3.0.9.
The model change is independent of the Agno upgrade PR #234 and does not change dependencies.

## Scope

Reviewer contract 14 uses the configured DeepSeek model (`deepseek-v4-flash` locally),
with thinking disabled, JSON-object output and the existing EvidenceReviewDraft parser.
The contract-13 published prompt is preserved during this model-only migration.
Transport timeout remains 120 seconds and retry settings are unchanged.
Raw Collection Workflow steps, sources, article identity, validation, publication,
and schedule configuration are unchanged. No UAT deployment was performed.

## Live run

- Workflow run: `80ee0bde-9867-47fa-8ddf-115087648fbd`.
- Session: `2aa4dc08-26d3-4e48-b67a-1aebba0028a1`.
- Published Reviewer version: 43; Workflow version: 39 referencing Reviewer 43.
- Started: 2026-09-10 16:17:44 Asia/Shanghai.
- Actual Workflow duration: **363.907 seconds**, approximately **6 minutes 4 seconds**.
- Final status: COMPLETED. A 30-second observer poll confirmed completion at 16:24:15;
  its 391.6-second observation interval is not the Workflow's actual execution duration.
- The run used the existing scheduled collection prompt, submitted once over REST with
  `background=true`. The local Schedule remained disabled throughout.
- Pending queue before and after: 0. No prior backlog was included.

## Business results

| Result | Count |
| --- | ---: |
| Acquisition batches | 24 |
| Collected candidates | 120 |
| Already-known article versions | 20 |
| Newly enqueued articles | 100 |
| Reviewer steps | 100 |
| Completed article publications | 66 |
| Excluded articles | 32 |
| Failed articles | 2 |
| Published Evidence entries reported by publication receipts | 258 |

All 66 publication manifests existed and parsed successfully. No publication was
marked as a reused publication. The two failures were Reviewer ValidationErrors
for malformed JSON; the existing per-article isolation recorded them and continued.
COMPLETED therefore means the workflow drained its queue, not that every article
succeeded. Failed articles were not automatically replayed.

This one run does not establish a speedup over Sol: there was no same-input,
same-state comparison. It also still exceeded the 300-second scheduler lease,
so changing models alone is not a demonstrated fix for duplicate scheduling.

## Verification and local checkout

- Model/Workflow published configuration readback confirmed DeepSeek and the pinned version.
- Full REST Workflow run completed; MCP handshake, component visibility and Assistant call passed
  using the MCP 2.x smoke client from PR #234.
- Format and type checks passed (114 source files) with this branch's pinned dependencies.
- 17 tests passed; two new tests cover non-thinking JSON configuration and prompt-preserving,
  idempotent migration. The new tests passed again after type-narrowing cleanup.
- The main checkout's pre-existing uncommitted changes were preserved. For the local experiment,
  a Compose override at `/tmp/agno-235-local.override.yaml` mounts this worktree's
  `agents/title_curator.py` over the existing application mount. Keep the worktree available
  until this change is merged and the normal checkout is updated.
- Temporary monitoring scripts were removed; business publications/artifacts remain as run evidence.

Rollback the model by recreating the local agentos service without that override using
`docker compose -f compose.yaml up -d --no-build agentos`; startup reconciles the old
Reviewer contract and Workflow reference. This does not undo the published business data.

DeepSeek integration references:
[JSON output](https://api-docs.deepseek.com/guides/json_mode/) and
[thinking toggle](https://api-docs.deepseek.com/guides/thinking_mode/).
