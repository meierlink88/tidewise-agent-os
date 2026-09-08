# Raw Collection V2

## Scope

`raw-collection-v2` is an additional code-owned Workflow in `workflows/raw_collection_v2.py`.
Its business logic lives in `capabilities/collection_v2/{functions,internal}`. The existing Raw Collection,
Evidence Extraction, Event Extraction, Investment Reasoning, Studio versions and Schedules are unchanged.
V2 has no default Schedule. It can be invoked manually over REST/MCP or explicitly scheduled by an operator later.

```text
original query
  -> Collect Raw V2 (Function)
       freeze active Source Snapshot -> existing Web Search/API/RSS adapters -> complete local batches
  -> Publish Raw V2 (Function)
       exact article-version deduplication -> frozen Markdown -> MinIO -> local archive receipt / run manifest
```

No Agent or LLM participates. There is no query decomposition, semantic relevance filtering, category lookup,
Evidence extraction, Data Raw/Evidence publication, Event queue marker or handoff to an Evidence consumer.
A successfully archived document need not be relevant to investment research.

## Acquisition and identity

The existing Collection public facade exposes stateless provider, URL, Markdown and MinIO interfaces.
V2 reuses their implementations without invoking the old Workflow Functions or touching their storage.
The original query is passed directly to query-capable adapters; fixed latest feeds retain their existing behavior.
Query validation remains 1–512 characters at the acquisition boundary. The same Source Snapshot ownership,
channel priority, bounded concurrency, timeout, provider result limits and per-channel error isolation apply.
At most one enabled Web Search Source is allowed. There is no added publication-time cutoff or full-page fetch.
Provider content therefore retains its existing form: full text, excerpt, summary or feed content depending on source.
Source credentials/configuration stay in memory; batches and sanitized channel receipts are persisted.

Identity follows the current per-article pipeline, not the retired title/simhash filter:

- canonical URL + SHA-256 of stripped body generates the existing `agentos.raw-evidence.v1:` publication key;
- same URL/body version is reused across runs; changed body at the same URL is a new version;
- different source URLs remain independent, even with equal titles or bodies;
- collection timestamps and Candidate IDs do not change article identity;
- the first Candidate metadata and rendered Markdown are frozen before upload and reused on subsequent attempts.

## Storage and ordering

Local root: `COLLECTOR_V2_ARTIFACT_ROOT`, default `data/collector_v2`.
Use a persistent volume and keep this root separate from the old collector/Evidence roots.

```text
runs/<run-id>/batches/<batch-id>.json  complete provider responses
runs/<run-id>/acquisition.json        complete acquisition snapshot without credentials
runs/<run-id>/manifest.json           final per-run dispositions and counts
articles/<article-key>/document.json frozen source metadata and MinIO pointer
articles/<article-key>/original.md   verified frozen document
articles/<article-key>/archived.json successful upload receipt
articles/<article-key>/last-error.json sanitized last failed upload attempt, if any
```

Objects use the existing configured `RAW_EVIDENCE_BUCKET` (default `raw-evidence`) and a separate key prefix:
`collection-v2/documents/<article-key>.md`. The environment-neutral pointer is `/{bucket}/{object_key}`.
The workflow uses existing MinIO credentials and bucket provisioning; it does not create a bucket or change its policy.

A per-article filesystem lock covers freezing, checksum validation, upload and receipt creation. This coordinates
concurrent runs sharing one persistent filesystem. It is not a distributed lease for independent local disks.
Only a successful MinIO upload earns `archived.json`. Run manifests record all dispositions after processing.
The new manifests live outside the old manifest index and are not consumable by either existing Evidence entry point.
There is no migration, transfer of pending articles or backfill in this change.

## Failures and replay

Invalid/empty articles are counted as invalid. A channel failure does not discard successful sibling results.
An object upload failure records a sanitized failure and processing continues with the next article.
Failure to safely persist or validate local state aborts the run; it is not reported as a successful archive.
Aggregate outcome is `completed`, `partial`, `failed` or `no_change`, with separate article/channel failure counts.
An Agno completed run means the stages finished; callers must inspect the business outcome and counts.

There are no framework retries or background recovery jobs. Repeating a completed run ID returns its saved manifest.
A later collection that reacquires a failed version may attempt its deterministic upload again using the frozen bytes.
If upload succeeded but saving the receipt failed, the existing MinIO adapter verifies identical object metadata and
reuses it on the next attempt. An immutable object conflict is never overwritten. Old error records are retained for audit.
If acquisition is interrupted before its final snapshot, retained batches remain audit artifacts; a new run performs
fresh acquisition. V2 does not attempt to recover or process historical batches automatically.

## Verification and use

```bash
source .venv/bin/activate
python -m unittest discover -s tests -p 'test_collection_v2*.py' -v
./scripts/validate.sh
curl -sSf http://localhost:8000/workflows/raw-collection-v2/runs \
  -F 'message=采集最新政经资讯' -F 'stream=false'
```

MCP: `run_workflow` with `workflow_id="raw-collection-v2"` and the original query as `message`.
The interface tests run real REST and streaming MCP against an isolated AgentOS/SQLite instance with fake source
and object-store boundaries. They never restart the existing container or publish synthetic material to business services.
Live external-source/MinIO acceptance is a separate manual run after deploying this branch through normal review.

Rollback removes the added Workflow registration/code after pausing any manually created V2 Schedule.
Retain the V2 artifacts and objects; no old Workflow or queue rollback is required.
