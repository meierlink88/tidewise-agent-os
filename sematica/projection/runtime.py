"""Private runtime configuration and Graphiti provider composition."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from pydantic import (
    AnyHttpUrl,
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = REPO_ROOT / ".runtime" / "graphiti.env"
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")
GRAPHITI_GROUP_ID = "neo4j"
REASON_SERVICE_ENV_KEYS = frozenset(
    {
        "REASON_API_SERVICE_TOKEN",
        "REASON_STATE_PATH",
        "REASON_WORKER_POLL_INTERVAL_SECONDS",
        "REASON_WORKER_BATCH_SIZE",
        "TIDEWISE_DATA_BASE_URL",
        "TIDEWISE_DATA_SERVICE_TOKEN",
    }
)


class ProjectionError(RuntimeError):
    """A fail-closed projection configuration or data-contract error."""


class GraphitiProviderConfig(BaseModel):
    """Provider values shared by authoritative projections and Episode ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    neo4j_user: str = Field(alias="NEO4J_USER", min_length=1)
    neo4j_password: SecretStr = Field(alias="NEO4J_PASSWORD")
    neo4j_bolt_port: int = Field(alias="NEO4J_BOLT_PORT", ge=1, le=65535)
    neo4j_http_port: int = Field(alias="NEO4J_HTTP_PORT", ge=1, le=65535)
    neo4j_uri_override: AnyUrl | None = Field(default=None, alias="NEO4J_URI")
    graphiti_llm_api_key: SecretStr = Field(alias="GRAPHITI_LLM_API_KEY")
    graphiti_llm_base_url: AnyHttpUrl = Field(alias="GRAPHITI_LLM_BASE_URL")
    graphiti_llm_model: str = Field(alias="GRAPHITI_LLM_MODEL", min_length=1)
    graphiti_embedding_api_key: SecretStr = Field(alias="GRAPHITI_EMBEDDING_API_KEY")
    graphiti_embedding_base_url: AnyHttpUrl = Field(alias="GRAPHITI_EMBEDDING_BASE_URL")
    graphiti_embedding_model: str = Field(alias="GRAPHITI_EMBEDDING_MODEL", min_length=1)
    graphiti_embedding_dim: int = Field(alias="GRAPHITI_EMBEDDING_DIM", ge=1, le=65536)

    @field_validator(
        "neo4j_password",
        "graphiti_llm_api_key",
        "graphiti_embedding_api_key",
    )
    @classmethod
    def secret_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("secret must not be blank")
        return value

    @property
    def neo4j_uri(self) -> str:
        if self.neo4j_uri_override is not None:
            return str(self.neo4j_uri_override).rstrip("/")
        return f"bolt://127.0.0.1:{self.neo4j_bolt_port}"


class RuntimeConfig(GraphitiProviderConfig):
    """Runtime values shared by Graphiti and the Tidewise Data API client."""

    tidewise_data_base_url: AnyHttpUrl = Field(alias="TIDEWISE_DATA_BASE_URL")
    tidewise_data_service_token: SecretStr = Field(alias="TIDEWISE_DATA_SERVICE_TOKEN")

    @field_validator("tidewise_data_service_token")
    @classmethod
    def data_service_secret_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("secret must not be blank")
        return value


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ProjectionError(f"missing runtime environment: {path}")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProjectionError("runtime environment must have mode 0600")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProjectionError(f"invalid environment entry at line {line_number}")
        key, value = line.split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in values:
            raise ProjectionError(f"invalid or duplicate environment key at line {line_number}")
        values[key] = value
    return values


def load_config(path: Path | None = None) -> RuntimeConfig:
    """Load the private runtime file without exporting credentials to child processes."""

    values = _parse_env(path or DEFAULT_ENV_FILE)
    declared = {field.alias for field in RuntimeConfig.model_fields.values()}
    unknown = set(values).difference(declared, REASON_SERVICE_ENV_KEYS)
    if unknown:
        raise ProjectionError(f"invalid runtime fields: {', '.join(sorted(unknown))}")
    try:
        return RuntimeConfig.model_validate({key: value for key, value in values.items() if key in declared})
    except ValidationError as exc:
        fields = sorted({str(item["loc"][0]) for item in exc.errors()})
        raise ProjectionError(f"invalid runtime fields: {', '.join(fields)}") from None


def load_graphiti_config(path: Path | None = None) -> GraphitiProviderConfig:
    """Load only Graphiti provider values from the shared private runtime file."""

    values = _parse_env(path or DEFAULT_ENV_FILE)
    declared = {field.alias for field in GraphitiProviderConfig.model_fields.values()}
    known = {field.alias for field in RuntimeConfig.model_fields.values()}.union(REASON_SERVICE_ENV_KEYS)
    unknown = set(values).difference(known)
    if unknown:
        raise ProjectionError(f"invalid runtime fields: {', '.join(sorted(unknown))}")
    try:
        return GraphitiProviderConfig.model_validate({key: value for key, value in values.items() if key in declared})
    except ValidationError as exc:
        fields = sorted({str(item["loc"][0]) for item in exc.errors()})
        raise ProjectionError(f"invalid runtime fields: {', '.join(fields)}") from None


def _unwrap_schema_properties(result: dict, response_model) -> dict:
    if response_model is None:
        return result
    required = set(response_model.model_fields)
    wrapped = result.get("properties")
    if not required.issubset(result) and isinstance(wrapped, dict) and required.issubset(wrapped):
        return wrapped
    return result


class DeepSeekCompatibleClient(OpenAIGenericClient):
    """Keep Graphiti structured responses compatible with DeepSeek JSON-object mode."""

    async def _generate_response(self, messages, response_model=None, max_tokens=16384, model_size=None):
        instruction = (
            "\n\nReturn one data instance that satisfies the schema. Do not return or describe "
            "the schema itself, its properties, descriptions, or JSON types."
        )
        if response_model is not None and instruction not in messages[-1].content:
            messages[-1].content += instruction
        kwargs = {"max_tokens": max_tokens}
        if model_size is not None:
            kwargs["model_size"] = model_size
        result = await super()._generate_response(messages, response_model, **kwargs)
        normalized = _unwrap_schema_properties(result, response_model)
        if response_model is not None:
            try:
                response_model.model_validate(normalized)
            except ValidationError:
                raise json.JSONDecodeError("structured response contract mismatch", "", 0) from None
        return normalized


def create_graphiti(config: GraphitiProviderConfig) -> Graphiti:
    """Compose pinned Graphiti with the configured Neo4j, LLM and embedder providers."""

    llm_config = LLMConfig(
        api_key=config.graphiti_llm_api_key.get_secret_value(),
        base_url=str(config.graphiti_llm_base_url).rstrip("/"),
        model=config.graphiti_llm_model,
        small_model=config.graphiti_llm_model,
        temperature=0,
        max_tokens=8192,
    )
    return Graphiti(
        uri=config.neo4j_uri,
        user=config.neo4j_user,
        password=config.neo4j_password.get_secret_value(),
        llm_client=DeepSeekCompatibleClient(
            config=llm_config,
            max_tokens=8192,
            structured_output_mode="json_object",
        ),
        embedder=OpenAIEmbedder(
            OpenAIEmbedderConfig(
                api_key=config.graphiti_embedding_api_key.get_secret_value(),
                base_url=str(config.graphiti_embedding_base_url).rstrip("/"),
                embedding_model=config.graphiti_embedding_model,
                embedding_dim=config.graphiti_embedding_dim,
            )
        ),
        cross_encoder=OpenAIRerankerClient(config=llm_config),
        max_coroutines=2,
    )
