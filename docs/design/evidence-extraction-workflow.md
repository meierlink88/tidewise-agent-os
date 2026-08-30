# Evidence Extraction Workflow

## Outcome

Incrementally consume completed Raw Collection manifests, classify each accepted Raw Evidence against the formal
Evidence Category Catalog, extract minimum complete business propositions in the same AI reading, publish Raw Evidence metadata and Evidence
through Data Service APIs, and advance a crash-safe file checkpoint only after both publications and the local
Evidence manifest and the downstream Event queue item succeed. The AI reads the verified local Markdown body, while
Data Service receives only its MinIO URL path through the existing `raw_text` field.

AgentOS owns the stable `publication_key` used for retries but does not create formal Raw Evidence or Evidence IDs.
Data Service returns `id` from Raw Evidence Publication; AgentOS passes that exact value as `raw_evidence_id` to
Evidence Publication. The complete Evidence response retains the unordered `ids` compatibility set and returns
`items[{input_index,id}]`, where `input_index` identifies the matching item in the current request.

## Runtime shape

```text
indexes/manifest-index.jsonl + data/evidence/checkpoint.json
  -> extract-evidences
       -> prepare-raw-document       (deterministic Function)
       -> prepare-evidence-analysis  (deterministic Function, run-scoped Category Catalog)
       -> analyze-raw-evidence       (Studio-managed Evidence Extractor Agent)
       -> validate-evidence-analysis (deterministic canonicalization, time normalization, Category code -> ID)
  -> publish-evidences
       -> publish-evidence-set       (deterministic Function, Data Service + manifest + Event queue + checkpoint)
```

The two business stages run inside an Agno `Loop` with a 100-document safety cap. `prepare-raw-document` returns
`stop=True` when no indexed work remains, which ends the loop without invoking the Agent.

The Workflow owns the durable Agno session. The Evidence Extractor remains a PostgreSQL/Studio-managed component,
but the copy rehydrated into the Agent Step has no independent session database and therefore cannot overwrite the
Workflow session row that stores run and step details. Running the Agent directly through AgentOS still resolves its
published database configuration and keeps normal Agent session persistence.

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

- The Agent owns exactly one Catalog-backed Category code, business-proposition splitting, exclusions,
  originality/quotation judgment, one concise `summary`, Evidence-owned keywords, and the structured semantic draft.
- Functions own Catalog retrieval and freezing, Category code-to-ID mapping, file parsing, trust-boundary validation,
  `publication_key`, API timeouts, response validation, Artifact ordering, and checkpoint transitions. Formal IDs are
  recorded in the final manifest, but Data publication responses do not become separate local receipts.
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
- Each Evidence publication item contains exactly `summary`, `keywords` and `semantic`. `semantic` contains exactly
  `actors`, `action`, `objects`, `stage`, `modality`, `time`, `jurisdictions`, `reason`, `method`, `metrics` and
  `attribution`.
- One minimum complete business proposition is one Evidence. Media attribution does not replace the business actor;
  title/lead/body repetition collapses; related metrics in one disclosure stay together; actual results and future
  guidance may split when stage or modality differs.
- The Workflow trims and canonically orders semantic collections, normalizes only exact source time expressions into
  UTC using Asia/Shanghai as the business timezone, collapses exact duplicates inside one document, and fails closed
  when the same business identity carries divergent content. Cross-source Evidence is never merged here.
- The second publication uses only the `id` returned by the first response. Its response must repeat that identity and
  return exactly one unique formal Evidence ID per submitted item. AgentOS treats `ids` as an unordered compatibility
  set. A one-item publication has one unambiguous local association; a multi-item publication requires the complete
  zero-based `items[{input_index,id}]` mapping. Missing multi-item, duplicate, out-of-range, or contradictory mappings
  fail before checkpoint advance.
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
- New frozen publications use `prepared_evidence_publication.v5`. A mapped publication writes immutable
  `evidence_identity_bindings.v1`, then completes `evidence_extraction_manifest.v5` last. The binding records the Raw
  Evidence identity, publication identity, document digest, Evidence count, and every request index-to-ID association.
- Data Service must return `items[{input_index,id}]` for every submitted Evidence. Responses without the complete
  mapping fail closed; this contract has no v4 Artifact fallback or mixed-version compatibility window. A completed
  v5 manifest and its frozen `prepared.json` remain recovery truth, and recovery validates its binding Artifact before
  advancing the checkpoint.
- Other domains consume resolved Evidence through the public Evidence capability reader, which returns the formal ID,
  Raw Evidence ID, summary, Evidence keywords and canonical business semantic content. They do not join Artifact files
  or infer identity order.
- Evidence keywords contain 1 to 5 ordered unique values, each at most 6 Unicode characters. Raw Evidence has no keywords.
- Article publication time never substitutes for Evidence fact time.
- `split_order`, `layer_type`, all Evidence `source_*`/`source_*_core` fields, `expression_fingerprint`,
  `expression_key`, and `fingerprint_version` are not part of the contract.
- Formal Raw Evidence and Evidence IDs are Data Service outputs.
- After the Evidence Artifact manifest exists, Evidence publication writes one idempotent
  `data/event/evidence-queue/pending/<evidence-id>.json` marker per formal Evidence. The marker stores only the formal
  Evidence ID, immutable manifest path, and creation time.
- The Evidence checkpoint advances only after every Event queue marker exists. Recovery recreates a missing marker
  from the immutable final Evidence Artifact before advancing, closing the lost-task window without PostgreSQL state.
