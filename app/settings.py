"""
App Settings
============

Shared runtime objects for the platform.
"""

from os import getenv

from agno.models.deepseek import DeepSeek
from agno.models.openai import OpenAIResponses

SOL_LOW_MODEL_ID = "gpt-5.6-sol"
SOL_LOW_DEFAULT_BASE_URL = "https://model-proxy.ceekeecloud.com/v1"
EVENT_DEEPSEEK_PROFILE = "EventDeepSeek-low"


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


def event_model() -> DeepSeek:
    """Event-only profile; SDK retries must not silently multiply request timeouts."""
    model = default_model()
    model.name = EVENT_DEEPSEEK_PROFILE
    model.use_thinking = True
    model.reasoning_effort = "low"
    model.timeout = 180
    model.retries = 0
    model.max_retries = 0
    return model


def is_event_model(model: object) -> bool:
    expected = event_model()
    return (
        isinstance(model, DeepSeek)
        and model.name == EVENT_DEEPSEEK_PROFILE
        and model.id == expected.id
        and model.base_url == expected.base_url
        and model.use_thinking == expected.use_thinking
        and model.reasoning_effort == "low"
    )


def sol_low_model() -> OpenAIResponses:
    """Return the registered GPT-5.6 Sol model with fixed low reasoning."""
    return OpenAIResponses(
        id=SOL_LOW_MODEL_ID,
        api_key=getenv("OPENAI_API_KEY"),
        base_url=getenv("OPENAI_BASE_URL", SOL_LOW_DEFAULT_BASE_URL),
        reasoning_effort="low",
        store=False,
    )


def is_sol_low_model(model: object) -> bool:
    """Return whether an Agent is bound to the code-owned Sol low profile."""
    return (
        isinstance(model, OpenAIResponses)
        and model.id == SOL_LOW_MODEL_ID
        and model.reasoning_effort == "low"
        and model.store is False
    )
