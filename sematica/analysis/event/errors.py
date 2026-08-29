"""Failure classes that control durable Event Analysis retry semantics."""


class PermanentEventAnalysisFailure(RuntimeError):
    """A validated identity or provenance invariant cannot succeed by retrying."""
