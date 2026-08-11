# Evidence Extraction Workflow

## Outcome

Incrementally consume completed Raw Collection manifests, extract atomic Evidence from each accepted document,
publish Raw Evidence and Evidence through Data Service APIs, and advance a crash-safe file checkpoint only after
both publications and the local Evidence manifest succeed.

## Runtime shape

```text
indexes/manifest-index.jsonl + data/evidence/checkpoint.json
  -> prepare-raw-document       (deterministic Function)
  -> extract-evidences          (Studio-managed Evidence Extractor Agent)
  -> validate-evidence-draft    (deterministic Function)
  -> publish-evidences          (deterministic Function, Data Service + manifest + checkpoint)
```

The four steps run inside an Agno `Loop` with a 100-document safety cap. `prepare-raw-document` returns
`stop=True` when no indexed work remains, which ends the loop without invoking the Agent.

## Ownership

- The Agent owns atomic semantic splitting, exclusions, originality/quotation judgment, SINGLE/DOUBLE, two-layer
  5W1H, keywords, and the readable expression fingerprint.
- Functions own file parsing, trust-boundary validation, IDs, split order, fingerprint keys, API timeouts,
  publication receipts, Artifact ordering, and checkpoint transitions.
- Data Service owns the formal Raw Evidence/Evidence facts and enforces its accepted V1 API contract.

## Invariants

- Evidence extraction reads the append-only manifest index by byte offset; it never scans historical document bodies.
- One Raw document is the atomic retry unit.
- Raw Evidence is published before its complete `1..N` Evidence set.
- Data Service calls have a three-second client timeout and reuse stable natural identities on retry.
- `SINGLE` has no core fields; `DOUBLE` requires `source_what_core`.
- Keywords contain 1 to 5 unique values, each at most 5 characters.
- Article publication time never substitutes for Evidence fact time.
- Evidence IDs, split order, expression keys, and fingerprint version are deterministic code outputs.
- The Evidence Artifact manifest is written last; checkpoint advances only after that manifest exists.
