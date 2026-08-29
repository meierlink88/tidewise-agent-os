"""Environment composition for the standalone ingestion API runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI
from pydantic import AnyHttpUrl, Field, SecretStr, field_validator

from sematica.analysis.event.adapters import GraphitiEventAnalysisLLM
from sematica.analysis.event.graphiti import (
    GraphitiCandidateRetriever,
    GraphitiSignalFactProjector,
)
from sematica.analysis.event.module import EventAnalysisModule
from sematica.analysis.event.pipeline import EventAnalysisPipeline
from sematica.analysis.event.review import ControlledSignalReviewer
from sematica.analysis.event.store import EventAnalysisStore
from sematica.ingestion.app import create_app
from sematica.ingestion.episcode.event.adapters import (
    CompositeEventHistory,
    DataEventClient,
    GraphitiLLMComparator,
)
from sematica.ingestion.episcode.event.pipeline import EventCandidatePipeline
from sematica.ingestion.episcode.event.resolver import EventResolver
from sematica.ingestion.episcode.event.stages.episode import GraphitiEpisodeStage
from sematica.ingestion.episcode.event.store import EventCandidateStore
from sematica.projection.runtime import GraphitiProviderConfig, create_graphiti


class IngestionRuntimeConfig(GraphitiProviderConfig):
    service_token: SecretStr = Field(alias="REASON_API_SERVICE_TOKEN")
    state_path: Path = Field(alias="REASON_STATE_PATH")
    tidewise_data_base_url: AnyHttpUrl = Field(alias="TIDEWISE_DATA_BASE_URL")
    tidewise_data_service_token: SecretStr = Field(alias="TIDEWISE_DATA_SERVICE_TOKEN")
    worker_poll_interval_seconds: float = Field(
        default=1.0,
        alias="REASON_WORKER_POLL_INTERVAL_SECONDS",
        gt=0,
    )
    worker_batch_size: int = Field(default=5, alias="REASON_WORKER_BATCH_SIZE", ge=1, le=50)

    @field_validator("service_token", "tidewise_data_service_token")
    @classmethod
    def service_token_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("service token must not be blank")
        return value


def load_ingestion_config(
    environment: Mapping[str, str] | None = None,
) -> IngestionRuntimeConfig:
    """Read only declared keys so unrelated process environment cannot enter the contract."""

    values = environment or os.environ
    payload: dict[str, str] = {}
    for field in IngestionRuntimeConfig.model_fields.values():
        alias = field.alias
        if alias is not None and alias in values:
            payload[alias] = values[alias]
    return IngestionRuntimeConfig.model_validate(payload)


def create_runtime_app(config: IngestionRuntimeConfig) -> FastAPI:
    event_pipeline, analysis_module, data = create_runtime_pipeline(config)
    return create_app(
        service_token=config.service_token.get_secret_value(),
        event_pipeline=event_pipeline,
        event_analysis_module=analysis_module,
        dependency_readiness=(data.ready, event_pipeline.ready),
        shutdown_callbacks=(event_pipeline.close,),
        worker_poll_interval_seconds=config.worker_poll_interval_seconds,
        worker_batch_size=config.worker_batch_size,
    )


def create_runtime_pipeline(config: IngestionRuntimeConfig):
    """Compose the one Event Candidate Pipeline shared by API and CLI."""

    graphiti = create_graphiti(config)
    data = DataEventClient(
        str(config.tidewise_data_base_url),
        config.tidewise_data_service_token.get_secret_value(),
    )
    analysis_llm = GraphitiEventAnalysisLLM(graphiti)
    analysis_pipeline = EventAnalysisPipeline(
        analysis_llm,
        GraphitiCandidateRetriever(graphiti),
        analysis_llm,
        ControlledSignalReviewer(),
        GraphitiSignalFactProjector(graphiti),
    )
    analysis_module = EventAnalysisModule(EventAnalysisStore(config.state_path), analysis_pipeline)
    resolver = EventResolver(
        CompositeEventHistory(graphiti, data),
        GraphitiLLMComparator(graphiti),
        data,
    )
    event_pipeline = EventCandidatePipeline(
        EventCandidateStore(config.state_path),
        resolver,
        GraphitiEpisodeStage(graphiti),
        analysis_module,
    )
    return event_pipeline, analysis_module, data
