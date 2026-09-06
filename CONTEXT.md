# Tidewise AgentOS Domain Context

## Ubiquitous language

### MacroEconomic

A Data-owned macroeconomic storyline with one core proposition and one primary macroeconomic domain.
The proposition describes how this topic transmits to China's economy and markets; it is not a current
investment prediction. The graph combines the MEC storyline with domain code/name/description and all
ordered reference tactics. Candidate assets are research references, not Event matching evidence.
Tactics are JSON-array text in Neo4j; assets are native string arrays. The domain FK stays in the
operator-exported joined snapshot, not on the graph node. AgentOS has no Data PostgreSQL runtime
dependency. Policy actions such as rate cuts are tactics, not standalone MacroEconomic nodes;
CountryImplementsMacroEconomic is retired. See docs/design/macroeconomic-graph-projection.md.

### GeopoliticRivalry

A Data-owned geopolitical storyline with one core proposition and one primary GeopoliticDomain.
Its graph node combines storyline facts with the domain code, name, description and full ordered tactics.
Candidate assets and main transmission are post-match research references. Event-facing summary excludes
these downstream references. Graph identity derives from the authoritative GPR ID. The local initialization
consumes an operator-exported joined Data snapshot; AgentOS has no Data PostgreSQL runtime dependency.
Neo4j stores tactics as JSON array text and candidate assets as a native string array.

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

### Event Association

A semantic judgment linking an Event's own actors, action and object to supplied authoritative
GeopoliticRivalry, MacroEconomic, IndustryChain/ChainNode or Company identities. Its primary class
selects the catalog, not its inferred downstream impact. Association is not a Signal and does not
prove a Variable changed. Functions validate selected IDs and write only existing-entity MENTIONS;
the new Event path never invokes free Graphiti entity extraction. Candidate assets are excluded
from matching inputs. Each scheduled run claims one bounded Evidence batch and processes its
Events through four class-specific Parallel branches using complete batch contexts.
Semantic Agents remain direct Steps; a class Loop prepares Signals after matching and a
deterministic publication Loop writes the frozen package. Code gates skip duplicate,
empty or already-completed work. See docs/design/event-extraction-workflow.md for v16.

### Event Identity

The rule for deciding whether two Event descriptions denote the same real-world Event. It compares exactly five
dimensions: actors, action, objects, lifecycle stage and compatible Event Time. Reason, method, quantitative
observations, modality and jurisdiction enrich meaning but do not independently turn the descriptions into the same
or a different Event.

### Evidence Metric

A canonical quantitative observation supported by Evidence. It names the measure and retains the stated value or
change plus any unit and period. Repeated equivalent observations may be merged deterministically, but their supported
business meaning must not be discarded.

### Signal Fact

A Graphiti Fact recording the direction, horizon, magnitude and confidence of one predefined Variable on one real
analysis anchor. It is authoritative retrieved graph data and remains a Signal Fact throughout reasoning; the
Workflow must not ask an LLM to restate it as a second Claim before it can be used.

### Layer Assessment

A Workflow-local interpretation of the direct Signal Facts retrieved for one real geopolitical, macroeconomic or
industry-chain-node anchor. It summarizes what those Signals imply for the layer while referencing, rather than
copying or replacing, the Signal Facts and their root Events.

### Transmission Hypothesis

A Workflow-local inferred path between layer assessments or industry-chain nodes. It is distinct from a direct
Signal Fact. An ordinary Fact may close its mechanism and a topology edge may constrain its path, but weak support is
recorded as low confidence or pending verification instead of being promoted to graph fact.

### Retrieval Receipt

The auditable record that one reasoning stage executed its required Graphiti retrieval actions. It lists the required
and completed actions, bounded queries, and retrieved Event, anchor, Fact and direct Signal identifiers. Finalization
validates this execution contract and verifies that output references stay inside the retrieved context.

### Investment Conclusion Artifact

One immutable, durable product result emitted after an `investment-reasoning` Workflow run has completed its required
retrieval actions and passed output-schema and reference-boundary validation. Reviewer findings about weak individual
hypotheses remain semantic audit notes and do not erase unrelated direct-Signal assessments. The Artifact contains the
proposition, final conclusion, layered and node-level assessments, support lineage, risks, limitations, and a context
fingerprint. Its identity is the Agno `workflow_run_id`; an identical retry is allowed, while conflicting content must
never overwrite it.

### Workflow Run Record

The Agno-owned execution and audit record for one Workflow run. It contains lifecycle state and Step results needed for observability, diagnosis, and retries. It is not the user-facing Investment Conclusion Artifact, even though its final `content` references the same finalized conclusion contract.
