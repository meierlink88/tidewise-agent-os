"""Runtime seam for the five-stage investment Workflow."""

from __future__ import annotations

from typing import Protocol

from capabilities.investment.internal.models import (
    AnalysisDraft,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    ReviewResult,
    TransmissionBatch,
)


class InvestmentWorkflowRuntime(Protocol):
    async def prepare(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext: ...

    async def propagate(
        self,
        context: InvestmentAnalysisContext,
        accepted: list,
        *,
        round_number: int,
    ) -> TransmissionBatch: ...

    async def synthesize(self, context: InvestmentAnalysisContext, transmissions: list) -> AnalysisDraft: ...

    async def review(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list,
        draft: AnalysisDraft,
    ) -> ReviewResult: ...

    async def repair(
        self,
        context: InvestmentAnalysisContext,
        transmissions: list,
        draft: AnalysisDraft,
        review: ReviewResult,
    ) -> AnalysisDraft: ...

    async def close(self) -> None: ...


_runtime: InvestmentWorkflowRuntime | None = None


def configure_investment_workflow_runtime(runtime: InvestmentWorkflowRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def investment_workflow_runtime() -> InvestmentWorkflowRuntime:
    if _runtime is None:
        raise RuntimeError("Investment Workflow runtime is not configured")
    return _runtime
