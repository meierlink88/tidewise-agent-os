# Per-article Raw Collection and Evidence Workflow

Contract 21 exposes exactly four Steps and one Loop, with no Condition nodes:

1. Evidence Collect — evidence_collect Function: acquire, retain originals, deduplicate article versions and enqueue.
2. process_articles Loop:
   - Prepare Evidence Review — prepare_evidence_review Function: select one unreviewed article and its category catalog.
   - Evidence Reviewer — direct Agent Step (title-curator, contract 11); the only LLM call.
   - Evidence Publish — evidence_publish Function: save the review, isolate exclusions, validate/deduplicate and publish.

The next article starts only after the current one is excluded or published. Business branches live inside Functions.
There is no receipt-only Step, recovery Loop, hidden Agent call, or automatic publication retry.

The Agent echoes article_key and returns is_relevant plus extraction. Irrelevant means extraction=null;
relevant means the unchanged EvidenceExtractionDraft. The Evidence prompt is reused from the existing seed file.
Canonicalization, fact-time conversion and within-document identity deduplication remain in the Evidence capability.
Actors/action/objects must be source-grounded; plans and speculation remain valid with appropriate modality/stage.
Zero canonical Evidence is excluded. Transport, JSON/envelope and category errors are recorded as failures, not exclusions.

## Identities and storage

Exact article identity is canonical URL plus stripped-body SHA-256, preserving the existing publication_key.
Same URL with changed content is a new version; matching or similar titles are not hard exclusions.
Different sources retain independent Evidence. Cross-source event identity remains the Event capability's job.

The Collection-owned article-queue stores immutable article.json/original.md, review.json, publication.json,
and state per identity. pending markers identify work. excluded markers record IRRELEVANT/NO_VALID_EVIDENCE and
point to the retained original; excluded originals are never uploaded or published by this path.
completed markers retain returned formal identities. State, not folder scanning of original bodies, controls routing.
Saved review/publication content remains available for audit; MinIO and Data retain existing idempotency contracts.
The current Workflow never selects saved unfinished review/publication attempts for automatic replay.
Evidence final manifests and ID bindings remain unchanged, and Event markers are completed before article completion.
Article publishing does not advance the retired global manifest cursor.

Agno 3.0.1's update_agents_and_teams_session_info only visits top-level Steps (its source contains a nested-primitive
TODO). app/workflow_runtime.py narrowly binds workflow_id for the Evidence Reviewer inside the Loop,
allowing native Agent storage to defer session ownership to the Workflow. Standalone Agent persistence is unchanged.
The shim is installed with the Registry, has SQLite/runtime coverage, and must be revisited on an Agno upgrade.
Agent pre-hooks cannot substitute here: this installed version does not serialize/reload them in component configs.

File-lock protected enqueue and claims prevent concurrent claim of an article. Claims expire after 15 minutes and
carry unique fencing tokens; API model timeout is 120 seconds. A crashed Agent step may hold its claim until expiry.
Deterministic-step failures mark the article failed and remove its pending marker, then raise to fail the Workflow.
Expired unfinished claims and historical saved attempts are recorded as failed on selection, not resumed or reread.
Reacquisition skips all terminal articles, including failed and excluded ones. Partial publication is possible:
Raw may exist without the full Evidence set when an API fails; operators inspect the exception, not an auto-recovery job.
Historical function names remain registered only so prior stored Workflow versions can still be inspected.

## Cutover and operations

Do not run the legacy Evidence consumer while transferring its cursor. Pause the Raw schedule and stop active consumers, then explicitly run
python -m scripts.cutover_article_workflow --consumers-stopped. This disables existing Evidence schedule rows
and enqueues legacy indexed Raw documents before advancing their cursor. Startup never changes schedules.
New schedule seeding no longer creates an Evidence Extraction schedule. Its Workflow remains available for historical
maintenance; normal acquisition creates no legacy Raw manifest entries and cannot be picked up by that reader.
The same command transfers staged Tool batches as well, retaining prior explicit irrelevant decisions as exclusions.
Repeated cutover is identity-idempotent. Existing originals and results are not deleted by the new contract migration.

Deploy only after runtime schema/registry round-trip, four-Step topology, duplicate/version handling, exclusion gates,
category errors and no-retry failure tests pass. Local REST uses isolated synthetic article evidence only; no test
article is to be published into a business Data Service. UAT cutover is a separate release operation.

Rollback requires pausing the new Raw workflow first. Keep article-queue and Evidence artifacts: reverting code alone
does not transfer its pending articles to the old manifest cursor. Restore the prior workflow version only with an
explicit backlog recovery plan; never delete the queue or blindly restore old scheduling.
