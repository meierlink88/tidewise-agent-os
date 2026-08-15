# Evidence Extraction Workflow

## Outcome

Incrementally consume completed Raw Collection manifests, classify each accepted Raw Evidence against the formal
Evidence Category Catalog, extract atomic Evidence in the same AI reading, publish Raw Evidence metadata and Evidence
through Data Service APIs, and advance a crash-safe file checkpoint only after both publications and the local
Evidence manifest succeed. The AI reads the verified local Markdown body, while Data Service receives only its MinIO
URL path through the existing `raw_text` field.

AgentOS owns the stable `publication_key` used for retries but does not create formal Raw Evidence or Evidence IDs.
Data Service returns `raw_evidence_id` from Raw Evidence Publication; AgentOS passes that exact value to Evidence
Publication, then records the returned `evidence_ids` in split order.

## Runtime shape

```text
indexes/manifest-index.jsonl + data/evidence/checkpoint.json
  -> prepare-raw-document       (deterministic Function)
  -> prepare-evidence-analysis  (deterministic Function, run-scoped Category Catalog)
  -> analyze-raw-evidence       (Studio-managed Evidence Extractor Agent)
  -> validate-evidence-analysis (deterministic Function, Category code -> ID)
  -> publish-evidences          (deterministic Function, Data Service + manifest + checkpoint)
```

The five steps run inside an Agno `Loop` with a 100-document safety cap. `prepare-raw-document` returns
`stop=True` when no indexed work remains, which ends the loop without invoking the Agent.

`prepare-evidence-analysis` calls `GET /api/data/v1/evidence-categories` only after work exists. The first document in
a Workflow Run freezes the complete, strictly validated Catalog in `RunContext.dependencies`; all later documents in
that run reuse the same snapshot. The Agent sees only each Category's `code`, `name`, and `description`. Formal IDs
remain outside the model context and are resolved deterministically after the Agent returns one `category_code`.
The final immutable `prepared.json` records the Catalog SHA-256, selected code, mapped ID, Agent semantics, and
publication payload so the decision remains auditable after the in-memory Run Context is gone. The full Data-owned
Catalog is never persisted by AgentOS; its complete snapshot exists only in `RunContext.dependencies` for that run.

The Catalog GET requires the caller scope `data.evidence-categories.read` in addition to the existing Raw Evidence and
Evidence publication scopes. Data Service must be deployed first and the AgentOS `DATA_SERVICE_TOKEN` principal must
receive that read scope before AgentOS is restarted onto this Workflow contract. Rolling AgentOS out first would make
scheduled Evidence Extraction runs fail at the Catalog boundary.

The explicit new-environment seed creates `evidence-extraction-every-10-minutes` with cron `*/10 * * * *`
in `Asia/Shanghai`. After seeding, PostgreSQL and Control Panel own the Schedule configuration; AgentOS startup
only validates the Workflow endpoint and never recreates or overwrites the row. Agno keeps a claimed schedule locked
until its Workflow run reaches a terminal state, so a slow scheduled run is not claimed again concurrently; later
runs continue from the file checkpoint.

## Ownership

- The Agent owns exactly one Catalog-backed Category code, atomic semantic splitting, exclusions,
  originality/quotation judgment, SINGLE/DOUBLE, two-layer 5W1H, keywords, and the readable expression fingerprint.
- Functions own Catalog retrieval and freezing, Category code-to-ID mapping, file parsing, trust-boundary validation,
  `publication_key`, split order, fingerprint keys, API timeouts, response validation, Artifact ordering, and
  checkpoint transitions. Formal IDs are recorded in the frozen publication or final manifest, but Data publication
  responses do not become separate local receipts.
- Data Service owns the Category Catalog and the formal Raw Evidence/Evidence facts, and enforces its accepted V1 API
  contracts.

## Invariants

- Evidence extraction reads the append-only manifest index by byte offset; it never scans historical document bodies.
- One Raw document is the atomic retry unit.
- One Workflow Run uses one immutable, complete Category Catalog snapshot. Empty, malformed, duplicated, unordered,
  or otherwise invalid Catalog responses fail before Agent analysis.
- Each Raw Evidence has exactly one Category in this Workflow contract. The Agent returns one exact Catalog code;
  deterministic validation maps it to one formal ID, and Raw Evidence Publication sends a single-element
  `category_ids` array.
- Raw Evidence is published before its complete `1..N` Evidence set.
- Raw Evidence Publication sends `publication_key` and no `raw_evidence_id`; Evidence items send no `evidence_id`.
- The second publication uses only the `raw_evidence_id` returned by the first response. Its response must repeat that
  identity and return exactly one unique formal Evidence ID per submitted `split_order`.
- Raw Evidence `raw_text` is the environment-neutral `/{bucket}/{object_key}` path recorded by Raw Collection, never
  the article body or a Base URL.
- Browser access is `environment MinIO Base URL + raw_text`; Data Service does not proxy the object.
- New `raw_collection_manifest.v2` entries require `url_path`. Unprocessed v1 manifests are skipped at the cutover
  without invoking AI or Data Service; each decision is recorded under `data/evidence/legacy-skips/` before the
  checkpoint advances. Historical Markdown remains local and is neither moved nor rewritten by this change.
- Data Service calls have a three-second client timeout. Raw Evidence retries reuse the stable `publication_key`; a
  retry after either successful remote phase repeats the same immutable payload and consumes the same formal IDs.
- The first prepared publication payload is frozen under `.pending`; retries reuse it even if a later Agent run emits
  different semantics, so an unknown remote outcome can never be retried with drifted content.
- New Evidence Artifact manifests use `evidence_extraction_manifest.v3`, are keyed locally by the SHA-256 of
  `publication_key`, and record the formal Raw Evidence ID plus the `split_order` to Evidence ID mapping. Historical
  v1/v2 manifests and pre-cutover `.pending/RAW_...` directories remain untouched and are not treated as formal IDs.
- A completed v3 manifest and its frozen `prepared.json` are the recovery truth if the process stops before checkpoint
  advancement; later Agent output cannot change the completed publication or prevent that checkpoint from advancing.
- `SINGLE` has no core fields; `DOUBLE` requires `source_what_core`.
- Keywords contain 1 to 5 unique values, each at most 5 characters.
- Article publication time never substitutes for Evidence fact time.
- Split order, expression keys, and fingerprint version are deterministic AgentOS outputs; formal Raw Evidence and
  Evidence IDs are Data Service outputs.
- The Evidence Artifact manifest is written last; checkpoint advances only after that manifest exists.
