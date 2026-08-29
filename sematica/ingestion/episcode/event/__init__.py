"""Event Candidate resolution, publication, and Graphiti projection Pipeline."""

__all__ = ["EventCandidatePipeline"]


def __getattr__(name: str):
    if name == "EventCandidatePipeline":
        from sematica.ingestion.episcode.event.pipeline import EventCandidatePipeline

        return EventCandidatePipeline
    raise AttributeError(name)
