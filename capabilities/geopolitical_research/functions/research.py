"""Studio functions: freeze story selection, then consume one story per Loop iteration."""

import json
from collections import Counter
from typing import Any

from agno.run import RunContext
from agno.workflow import StepInput, StepOutput

from capabilities.geopolitical_research.internal.execution import research_story
from capabilities.geopolitical_research.internal.models import ResearchPlan
from capabilities.geopolitical_research.internal.selection import prepare_plan
from capabilities.geopolitical_research.internal.storage import ResearchBusy, lock, run_root, write_json, write_text


async def select_geopolitical_stories(step_input: StepInput, run_context: RunContext) -> StepOutput:
    directory = run_root(run_context.run_id)
    with lock(directory / ".run.lock"):
        path = directory / "plan.json"
        if path.exists():
            plan = ResearchPlan.model_validate_json(path.read_text())
            if plan.workflow_run_id != run_context.run_id:
                raise ValueError("Research plan identity conflict")
        else:
            plan = await prepare_plan(run_context.run_id, step_input.input)
            write_text(path, plan.model_dump_json(indent=2))
        return StepOutput(
            content={
                "workflow_run_id": plan.workflow_run_id,
                "selected_stories": len(plan.stories),
                "start_at": plan.start_at.isoformat(),
                "cutoff_at": plan.cutoff_at.isoformat(),
                "plan_path": str(path),
                "story_ids": [story.story_id for story in plan.stories],
            }
        )


def _result(plan: ResearchPlan, items: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["status"] for item in items)
    done = len(items) == len(plan.stories)
    completed = counts["completed"]
    unresolved = len(items) - completed
    return {
        "schema_version": "geopolitical-research-result/v1",
        "workflow_run_id": plan.workflow_run_id,
        "done": done,
        "outcome": (
            "running"
            if not done
            else "no_change"
            if not items
            else "partial"
            if completed and unresolved
            else "failed"
            if unresolved
            else "completed"
        ),
        "selected_stories": len(plan.stories),
        "completed": completed,
        "unresolved": unresolved,
        "items": items,
        "manifest_path": str(run_root(plan.workflow_run_id) / "result.json"),
    }


async def research_next_geopolitical_story(step_input: StepInput, run_context: RunContext) -> StepOutput:
    directory = run_root(run_context.run_id)
    with lock(directory / ".run.lock"):
        plan = ResearchPlan.model_validate_json((directory / "plan.json").read_text())
        if plan.workflow_run_id != run_context.run_id:
            raise ValueError("Research plan identity conflict")
        path = directory / "result.json"
        saved = json.loads(path.read_text()) if path.exists() else None
        items = saved["items"] if saved is not None else []
        expected_ids = [story.story_id for story in plan.stories]
        if saved is not None and (
            saved.get("workflow_run_id") != plan.workflow_run_id
            or len(items) > len(expected_ids)
            or [item["story_id"] for item in items] != expected_ids[: len(items)]
        ):
            raise ValueError("Research result checkpoint does not match the frozen plan")
        if len(items) < len(plan.stories):
            story = plan.stories[len(items)]
            try:
                receipt = await research_story(plan, story)
            except ResearchBusy:
                receipt = {"story_id": story.story_id, "status": "running", "error_code": "research_busy"}
            # The full request lives in plan/receipt files; keep Workflow output as report references.
            items.append({key: value for key, value in receipt.items() if key != "user_vars"})
        result = _result(plan, items)
        write_json(path, result)
        return StepOutput(content=result)


def geopolitical_research_complete(outputs: list[StepOutput]) -> bool:
    return any(isinstance(output.content, dict) and output.content.get("done") is True for output in outputs)
