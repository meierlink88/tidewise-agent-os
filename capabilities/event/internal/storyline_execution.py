"""Lease cleanup for deterministic Workflow operations, not semantic orchestration."""

import inspect
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, NoReturn
from uuid import uuid4

from agno.run import RunContext

from capabilities.event.internal.models import EventExtractionBatch
from capabilities.event.internal.storage import release_event_batch_lease

logger = logging.getLogger(__name__)


class StorylineOperationError(RuntimeError):
    """Stable public failure without Provider bodies, prompts or validation input values."""

    def __init__(self, operation: str, error: Exception):
        self.operation = operation
        self.code = (
            "EVENT_CONTRACT_REJECTED" if isinstance(error, (ValueError, TypeError)) else "EVENT_OPERATION_FAILED"
        )
        self.diagnostic_id = str(uuid4())
        logger.error(
            "event_storyline_failure operation=%s code=%s diagnostic_id=%s error_type=%s",
            operation,
            self.code,
            self.diagnostic_id,
            type(error).__name__,
        )
        super().__init__(f"{self.code}: {operation}; diagnostic_id={self.diagnostic_id}")


def _fail(context: RunContext, function: Callable[..., Any], error: Exception) -> NoReturn:
    try:
        _release(context)
    except Exception as cleanup:
        logger.error("event_storyline_lease_cleanup_failed error_type=%s", type(cleanup).__name__)
    if isinstance(error, StorylineOperationError):
        raise error from None
    raise StorylineOperationError(function.__name__, error) from None


def _release(context: RunContext) -> None:
    state = (context.dependencies or {}).get("event_workflow_state")
    if not isinstance(state, dict):
        state = (context.session_state or {}).get("event_workflow_state")
    if isinstance(state, dict) and state.get("batch"):
        release_event_batch_lease(EventExtractionBatch.model_validate(state["batch"]))


def release_on_failure(function: Callable[..., Any]) -> Callable[..., Any]:
    """Preserve Agno's injectable signature and let a failed batch resume immediately."""
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(step_input, run_context: RunContext):
            try:
                return await function(step_input, run_context)
            except Exception as error:
                _fail(run_context, function, error)

        return asynchronous

    @wraps(function)
    def synchronous(step_input, run_context: RunContext):
        try:
            return function(step_input, run_context)
        except Exception as error:
            _fail(run_context, function, error)

    return synchronous
