"""Runtime seam for the five-stage investment Workflow."""

from __future__ import annotations

from typing import Protocol

from capabilities.investment.internal.models import (
    CrossLayerAnalysisResult,
    IndustryAnalysisState,
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    LayerAnalysisResult,
    PreparedInvestmentContext,
    ReviewResult,
)
from capabilities.investment.internal.report_contract import InvestmentReportArtifact
from capabilities.investment.internal.report_publication import (
    ReportPublicationReceipt,
    ReportPublicationRequest,
)


class InvestmentWorkflowRuntime(Protocol):
    async def prepare(self, request: InvestmentAnalysisRequest) -> InvestmentAnalysisContext: ...

    async def analyze_geopolitical(
        self,
        prepared: PreparedInvestmentContext,
    ) -> LayerAnalysisResult: ...

    async def analyze_macro(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
    ) -> tuple[LayerAnalysisResult, CrossLayerAnalysisResult]: ...

    async def analyze_industry(
        self,
        prepared: PreparedInvestmentContext,
        geopolitical: LayerAnalysisResult,
        macro: LayerAnalysisResult,
        macro_transmission: CrossLayerAnalysisResult | None = None,
    ) -> IndustryAnalysisState: ...

    async def review(
        self,
        state: IndustryAnalysisState,
    ) -> ReviewResult: ...

    async def repair(
        self,
        state: IndustryAnalysisState,
        review: ReviewResult,
    ) -> IndustryAnalysisState: ...

    async def write_and_edit_report(
        self,
        report: InvestmentReportArtifact,
    ) -> InvestmentReportArtifact: ...

    async def publish_report(self, request: ReportPublicationRequest) -> ReportPublicationReceipt: ...

    async def close(self) -> None: ...


_runtime: InvestmentWorkflowRuntime | None = None


def configure_investment_workflow_runtime(runtime: InvestmentWorkflowRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def investment_workflow_runtime() -> InvestmentWorkflowRuntime:
    if _runtime is None:
        raise RuntimeError("Investment Workflow runtime is not configured")
    return _runtime
