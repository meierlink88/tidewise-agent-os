"""One story, one durable remote run; known runs resume via GET, never a second POST."""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from capabilities.geopolitical_research.internal.client import ResearchClient
from capabilities.geopolitical_research.internal.models import PRESET, ResearchPlan, ResearchReceipt, ResearchStory
from capabilities.geopolitical_research.internal.storage import digest, lock, root, write_text


def job_key(plan: ResearchPlan, story: ResearchStory) -> str:
    # Query scope and transport contract are part of the report identity.
    return digest(
        json.dumps(
            {
                "version": 2,
                "preset": PRESET,
                "market": plan.market,
                "story": story.model_dump(mode="json"),
                "start": plan.start_at.isoformat(),
                "end": plan.cutoff_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def user_variables(plan: ResearchPlan, story: ResearchStory) -> dict[str, str]:
    return {
        "crisis": story.name,
        "market": plan.market,
        "story_id": story.story_id,
        "research_date": "",
        "agentos_workflow_run_id": plan.workflow_run_id,
        "event_window_start": plan.start_at.isoformat(),
        "event_window_end": plan.cutoff_at.isoformat(),
    }


def _save(path: Path, receipt: ResearchReceipt) -> None:
    receipt.updated_at = datetime.now(UTC)
    write_text(path, receipt.model_dump_json(indent=2))


def _seconds(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not 0 < value <= 86_400:
        raise ValueError(f"Invalid {name}")
    return value


async def research_story(plan: ResearchPlan, story: ResearchStory) -> dict[str, Any]:
    key = job_key(plan, story)
    directory = root() / "jobs" / key
    with lock(directory / ".lock"):
        path = directory / "receipt.json"
        receipt = ResearchReceipt.model_validate_json(path.read_text()) if path.exists() else None
        if receipt is not None:
            if receipt.job_id != key or receipt.story_id != story.story_id:
                raise ValueError("Research receipt identity conflict")
            if receipt.status == "completed":
                report = (directory / "report.md").read_bytes().decode("utf-8")
                if digest(report) != receipt.report_sha256:
                    raise ValueError("Archived report checksum mismatch")
                return {**receipt.model_dump(mode="json"), "reused": True}
            if receipt.status in {"failed", "cancelled", "unknown", "submitting"}:
                # A crash between POST and the run-ID checkpoint is indeterminate.
                return {**receipt.model_dump(mode="json"), "error_code": receipt.error_code or "dispatch_unknown"}
        timeout = _seconds("TIDEWISE_RESEARCH_WAIT_SECONDS", 7_200)
        interval = _seconds("TIDEWISE_RESEARCH_POLL_SECONDS", 5)
        client = ResearchClient()
        try:
            if receipt is None:
                receipt = ResearchReceipt(
                    job_id=key,
                    story_id=story.story_id,
                    source_workflow_run_id=plan.workflow_run_id,
                    research_base_url=client.base_url,
                    user_vars=user_variables(plan, story),
                    status="submitting",
                    updated_at=datetime.now(UTC),
                )
                _save(path, receipt)
                try:
                    receipt.research_run_id = await client.create(receipt.user_vars)
                except (httpx.HTTPError, ValueError):
                    receipt.status = "unknown"
                    receipt.error_code = "dispatch_unknown"
                    _save(path, receipt)
                    return receipt.model_dump(mode="json")
                receipt.status = "running"
                _save(path, receipt)
            elif receipt.research_base_url != client.base_url:
                raise ValueError("Research origin differs from the saved run origin")
            assert receipt.research_run_id is not None
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        detail = await client.get(receipt.research_run_id, receipt.user_vars)
                        if detail.status in {"failed", "cancelled"}:
                            receipt.status = detail.status
                            receipt.error_code = f"research_{detail.status}"
                            _save(path, receipt)
                            return receipt.model_dump(mode="json")
                        if detail.status == "completed":
                            if not detail.final_report or not detail.final_report.strip():
                                raise ValueError("Completed research has no final report")
                            report_path = directory / "report.md"
                            if report_path.exists() and report_path.read_bytes().decode("utf-8") != detail.final_report:
                                raise ValueError("Research report snapshot conflict")
                            write_text(report_path, detail.final_report)
                            receipt.status = "completed"
                            receipt.report_path = str(report_path)
                            receipt.report_sha256 = digest(detail.final_report)
                            receipt.report_bytes = len(detail.final_report.encode("utf-8"))
                            receipt.error_code = None
                            _save(path, receipt)
                            return receipt.model_dump(mode="json")
                        await asyncio.sleep(interval)
            except (TimeoutError, httpx.HTTPError):
                # Persist the known ID. A subsequent execution resumes GET only.
                receipt.error_code = "research_wait_interrupted"
                _save(path, receipt)
                return receipt.model_dump(mode="json")
            except ValueError:
                receipt.status = "failed"
                receipt.error_code = "research_contract_rejected"
                _save(path, receipt)
                return receipt.model_dump(mode="json")
        finally:
            await client.close()
