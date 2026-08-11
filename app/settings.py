"""
App Settings
============

Shared runtime objects for the platform.
"""

from os import getenv

from agno.models.deepseek import DeepSeek


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
