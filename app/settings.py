"""
App Settings
============

Shared runtime objects for the platform.
"""

from os import getenv

from agno.models.deepseek import DeepSeek
from agno.models.openai import OpenAIResponses

SOL_MEDIUM_MODEL_ID = "gpt-5.6-sol"
SOL_MEDIUM_DEFAULT_BASE_URL = "https://model-proxy.ceekeecloud.com/v1"


def _env_flag(name: str, default: bool) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_model() -> DeepSeek:
    """Fresh model instance per agent — avoids shared-state footguns."""
    return DeepSeek(
        id=getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_key=getenv("DEEPSEEK_API_KEY"),
        base_url=getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        use_thinking=_env_flag("DEEPSEEK_USE_THINKING", default=False),
    )


def sol_medium_model() -> OpenAIResponses:
    """Return the registered GPT-5.6 Sol model with fixed medium reasoning."""
    return OpenAIResponses(
        id=SOL_MEDIUM_MODEL_ID,
        api_key=getenv("OPENAI_API_KEY"),
        base_url=getenv("OPENAI_BASE_URL", SOL_MEDIUM_DEFAULT_BASE_URL),
        reasoning_effort="medium",
        store=False,
    )


def is_sol_medium_model(model: object) -> bool:
    """Return whether an Agent is bound to the code-owned Sol medium profile."""
    return (
        isinstance(model, OpenAIResponses)
        and model.id == SOL_MEDIUM_MODEL_ID
        and model.reasoning_effort == "medium"
        and model.store is False
    )
