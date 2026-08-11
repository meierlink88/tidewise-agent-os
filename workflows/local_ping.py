"""Deterministic, model-free local runtime health workflow."""

from agno.workflow.step import Step, StepInput, StepOutput
from agno.workflow.workflow import Workflow

from db import get_postgres_db


def local_ping_step(_step_input: StepInput) -> StepOutput:
    """Return a stable response without calling an LLM or external service."""
    return StepOutput(content="Tidewise AgentOS OK", success=True)


local_ping = Workflow(
    id="local-ping",
    name="Local Ping",
    db=get_postgres_db(),
    steps=[Step(name="local-ping", executor=local_ping_step)],
)
