"""CLI boundary for context freezing, DAG execution and executor comparison."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from sematica.projection.runtime import (
    GraphitiProviderConfig,
    create_graphiti,
    load_graphiti_config,
)
from sematica.reasoning.investment.adapters import (
    GraphitiInvestmentContextAssembler,
    GraphitiLLMInvestmentReasoner,
    RecordedInvestmentReasoner,
)
from sematica.reasoning.investment.comparison import compare_results
from sematica.reasoning.investment.contracts import (
    InvestmentAnalysisContext,
    InvestmentAnalysisRequest,
    InvestmentAnalysisResult,
    RecordedReasoningPayload,
    TransmissionBatch,
    TransmissionProposal,
)
from sematica.reasoning.investment.pipeline import InvestmentReasoningPipeline


def _read(path: str, model):
    return model.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _write(path: str, model) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def _graphiti_config() -> GraphitiProviderConfig:
    """Prefer injected service environment; fall back to the private local file."""

    aliases = {field.alias for field in GraphitiProviderConfig.model_fields.values()}
    values = {key: value for key, value in os.environ.items() if key in aliases}
    if aliases.issubset(values):
        return GraphitiProviderConfig.model_validate(values)
    return load_graphiti_config()


async def _build_context(request_path: str, output_path: str) -> None:
    graphiti = create_graphiti(_graphiti_config())
    try:
        context = await GraphitiInvestmentContextAssembler(graphiti).build(
            _read(request_path, InvestmentAnalysisRequest)
        )
        _write(output_path, context)
    finally:
        await graphiti.close()


async def _run_deepseek(context_path: str, output_path: str) -> None:
    graphiti = create_graphiti(_graphiti_config())
    try:
        context = _read(context_path, InvestmentAnalysisContext)
        result = await InvestmentReasoningPipeline(GraphitiLLMInvestmentReasoner(graphiti)).run(context)
        _write(output_path, result)
    finally:
        await graphiti.close()


async def _run_recorded(context_path: str, payload_path: str, output_path: str) -> None:
    context = _read(context_path, InvestmentAnalysisContext)
    payload = _read(payload_path, RecordedReasoningPayload)
    result = await InvestmentReasoningPipeline(RecordedInvestmentReasoner(payload)).run(context)
    _write(output_path, result)


async def _replay_result(context_path: str, result_path: str, output_path: str) -> None:
    context = _read(context_path, InvestmentAnalysisContext)
    source = _read(result_path, InvestmentAnalysisResult)
    rounds: dict[int, list[TransmissionProposal]] = {}
    for item in source.transmissions:
        rounds.setdefault(item.hop, []).append(
            TransmissionProposal.model_validate(item.model_dump(exclude={"transmission_id", "hop"}))
        )
    payload = RecordedReasoningPayload(
        executor_name=source.executor,
        rounds={number: TransmissionBatch(proposals=proposals) for number, proposals in rounds.items()},
        draft=source.draft,
        review=source.review,
        execution_issues=source.execution_issues,
    )
    result = await InvestmentReasoningPipeline(RecordedInvestmentReasoner(payload)).run(context)
    _write(output_path, result)


async def _review_result(context_path: str, result_path: str, output_path: str) -> None:
    graphiti = create_graphiti(_graphiti_config())
    try:
        context = _read(context_path, InvestmentAnalysisContext)
        source = _read(result_path, InvestmentAnalysisResult)
        reasoner = GraphitiLLMInvestmentReasoner(graphiti)
        review = await reasoner.review(
            context,
            source.transmissions,
            source.draft,
        )
        result = source.model_copy(
            update={
                "status": "SUCCEEDED" if review.accepted else "NEEDS_REVIEW",
                "review": review,
                "execution_issues": list(dict.fromkeys(source.execution_issues + reasoner.execution_issues)),
            }
        )
        _write(output_path, result)
    finally:
        await graphiti.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fixed investment reasoning DAG")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-context")
    build.add_argument("request")
    build.add_argument("output")
    deepseek = commands.add_parser("run-deepseek")
    deepseek.add_argument("context")
    deepseek.add_argument("output")
    recorded = commands.add_parser("run-recorded")
    recorded.add_argument("context")
    recorded.add_argument("payload")
    recorded.add_argument("output")
    replay = commands.add_parser("replay-result")
    replay.add_argument("context")
    replay.add_argument("result")
    replay.add_argument("output")
    review = commands.add_parser("review-result")
    review.add_argument("context")
    review.add_argument("result")
    review.add_argument("output")
    compare = commands.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("output")
    args = parser.parse_args()
    if args.command == "build-context":
        asyncio.run(_build_context(args.request, args.output))
    elif args.command == "run-deepseek":
        asyncio.run(_run_deepseek(args.context, args.output))
    elif args.command == "run-recorded":
        asyncio.run(_run_recorded(args.context, args.payload, args.output))
    elif args.command == "replay-result":
        asyncio.run(_replay_result(args.context, args.result, args.output))
    elif args.command == "review-result":
        asyncio.run(_review_result(args.context, args.result, args.output))
    else:
        left = _read(args.left, InvestmentAnalysisResult)
        right = _read(args.right, InvestmentAnalysisResult)
        _write(args.output, compare_results(left, right))


if __name__ == "__main__":
    main()
