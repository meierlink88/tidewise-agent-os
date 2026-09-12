# Live storyline research queries (Issue #245)

AgentOS owns read-only Event/Signal/Evidence queries. Research consumes MCP; it never receives DB credentials.
`query_story_events(story_id, research_date, after_event_id="", limit=100)` accepts authoritative GPR business IDs.
Dates are Asia/Shanghai calendar days, selected by Event graph `created_at` in [00:00, next 00:00).
This means newly inserted graph Events, not event occurrence, collection or association time. Late association
of an older Event is intentionally not included. There is no snapshot, export package or historical expansion.
Each page is a live read ordered by Event business ID. Follow `next_after_event_id` until null, deduplicate IDs;
concurrent updates may change totals/content and newly inserted IDs before the cursor require a fresh scan.

Events are selected by MENTIONS of the exact GeopoliticRivalry in the configured graph group. All associated
SIGNAL_ON facts are returned, including other anchor types and invalidated records, without relevance filtering.
Multi-source signals outside the full selected story/day set return only identity/source gaps, not assertion text.
No signal means none currently found, not no impact. Journal publication readiness and missing provenance are explicit.
`get_story_evidence(story_id, research_date, event_id, evidence_id)` verifies scope and references before returning
atomic Evidence semantic content; it does not promise complete news articles. Evidence IDs come from publication journals.

Agno MCPServerConfig registers both tools on the existing /mcp endpoint with existing authentication. Custom tools
require the framework-injected authenticated user ID outside dev; it is hidden from clients. REST routes
/research/story-events and /research/story-events/evidence share this boundary and existing auth middleware.
Missing identities/provenance are explicit, infrastructure failures are errors, never empty successful results.
Tools do not mutate graph, journal, schedules, signals, or publication. Whole-page captures are not transaction snapshots.

Acceptance: date boundaries, group/story scoping, complete pagination, source closure across pages, no historical
expansion, Evidence scope, authentication, REST and MCP schema/calls, then one native four-role research run.
No scheduler, automatic publication or automatic Signal writeback is added. Rollback removes the tool registrations,
router and research allowlists. Deploy provider before enabling consumer allowlists; missing MCP tools must fail preflight.

The server returns up to 100 complete Events and associated Signals per page, reducing the page at Event boundaries to fit 28,000 characters. A single oversized Event fails explicitly. The consumer grants only the two AgentOS tools 30,000 characters and enables compact MCP JSON serialization; other tools retain 10,000. This is not a guarantee that an arbitrary daily dataset always fits one page.
