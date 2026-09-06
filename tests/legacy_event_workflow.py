"""Frozen v13 topology fixture for regression tests of still-registered historical Functions."""

from agno.workflow import Loop, Step, Workflow
from agno.workflow.types import HumanReview, OnError

from capabilities.event.functions import (
    analyze_signals,
    event_extraction_complete,
    extract_events,
    publish_events,
    publish_signals,
    resolve_events,
)


def seed_legacy_workflow(extractor, identity, signal_analyst, *, agent_versions):
    # Historical Functions explicitly load their pinned Agents through the runtime seam.
    return Workflow(
        id="event-extraction",
        dependencies={},
        metadata={
            "event_extraction_contract_version": 13,
            "event_extraction_publication_policy": "code_managed_exact_agent_links.v1",
            "event_agent_versions": agent_versions,
        },
        steps=[
            Loop(
                name="Process Event Evidence batches",
                max_iterations=50,
                end_condition=event_extraction_complete,
                human_review=HumanReview(on_error=OnError.fail),
                steps=[
                    Step(
                        name=name,
                        step_id=step_id,
                        executor=executor,
                        max_retries=0,
                        human_review=HumanReview(on_error=OnError.fail),
                        strict_input_validation=True,
                    )
                    for name, step_id, executor in [
                        ("Extract Events", "event-extract", extract_events),
                        ("Resolve Events", "event-resolve", resolve_events),
                        ("Publish Events", "event-publish", publish_events),
                        ("Analyze Signals", "event-signal-analyze", analyze_signals),
                        ("Publish Signals", "event-signal-publish", publish_signals),
                    ]
                ],
            )
        ],
    )
