# Per-article Raw Collection and Evidence Workflow

Contract 20 replaces the daily two-workflow path. Acquisition persists complete Tool batches and enqueues
article versions. An Agno Loop prepares one article, conditionally calls the existing title-curator (contract 10),
saves its review, validates and deduplicates Evidence, then conditionally archives and publishes that article.
The next article starts only after the current one is excluded or published. There is no receipt-only Step.

The Agent echoes article_key and returns is_relevant plus extraction. Irrelevant means extraction=null;
relevant means the unchanged EvidenceExtractionDraft. The Evidence prompt is reused from the existing seed file.
Canonicalization, fact-time conversion and within-document identity deduplication remain in the Evidence capability.
Actors/action/objects must be source-grounded; plans and speculation remain valid with appropriate modality/stage.
Zero canonical Evidence is excluded. Transport, JSON/envelope and category errors fail for retry, not exclusion.

## Identities and storage

Exact article identity is canonical URL plus stripped-body SHA-256, preserving the existing publication_key.
Same URL with changed content is a new version; matching or similar titles are not hard exclusions.
Different sources retain independent Evidence. Cross-source event identity remains the Event capability's job.

The Collection-owned article-queue stores immutable article.json/original.md, review.json, publication.json,
and state per identity. pending markers identify work. excluded markers record IRRELEVANT/NO_VALID_EVIDENCE and
point to the retained original; excluded originals are never uploaded or published by this path.
completed markers retain returned formal identities. State, not folder scanning of original bodies, controls routing.
The frozen first publication survives retries; MinIO and both Data APIs retain their existing idempotency contracts.
Evidence final manifests and ID bindings remain unchanged, and Event markers are completed before article completion.
Article publishing does not advance the retired global manifest cursor.

Agno 3.0.1's update_agents_and_teams_session_info only visits top-level Steps (its source contains a nested-primitive
TODO). app/workflow_runtime.py narrowly binds workflow_id for the Raw Collection Reviewer inside Loop/Condition,
allowing native Agent storage to defer session ownership to the Workflow. Standalone Agent persistence is unchanged.
The shim is installed with the Registry, has SQLite/runtime coverage, and must be revisited on an Agno upgrade.
Agent pre-hooks cannot substitute here: this installed version does not serialize/reload them in component configs.

File-lock protected enqueue and claims prevent concurrent claim of an article. Claims expire after 15 minutes and
carry unique fencing tokens; API model timeout is 120 seconds. A crashed Agent step may hold its claim until expiry.
Deterministic-step failures release their claim immediately. A recovered publication or saved review bypasses the
Agent. A crash before the review is durably saved may require another model call; there is no exactly-once LLM promise.
An article already in a terminal state is skipped on reacquisition, including previous exclusions.

## Cutover and operations

Do not run the legacy Evidence consumer while transferring its cursor. Pause the Raw schedule and stop active consumers, then explicitly run
python -m scripts.cutover_article_workflow --consumers-stopped. This disables existing Evidence schedule rows
and enqueues legacy indexed Raw documents before advancing their cursor. Startup never changes schedules.
New schedule seeding no longer creates an Evidence Extraction schedule. Its Workflow remains available for historical
maintenance; normal acquisition creates no legacy Raw manifest entries and cannot be picked up by that reader.
The same command transfers staged Tool batches as well, retaining prior explicit irrelevant decisions as exclusions.
Repeated cutover is identity-idempotent. Frozen or completed historical Evidence is recovered without another reading.

Deploy only after runtime schema/registry round-trip, duplicate/version handling, exclusion gates, publication replay,
category errors and Event enqueue recovery pass. Local REST uses isolated synthetic article evidence only; no test
article is to be published into a business Data Service. UAT cutover is a separate release operation.

Rollback requires pausing the new Raw workflow first. Keep article-queue and Evidence artifacts: reverting code alone
does not transfer its pending articles to the old manifest cursor. Restore the prior workflow version only with an
explicit backlog recovery plan; never delete the queue or blindly restore old scheduling.
