"""LLM factory — picks provider based on available API keys.

Priority when both keys present: Anthropic first (higher quality for long-form tasks).
Caller can override with explicit provider/model.
"""
import os
from functools import lru_cache

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")

ANTHROPIC_MODELS = [
    {"id": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6",  "provider": "anthropic"},
    {"id": "claude-opus-4-7",            "label": "Claude Opus 4.7",    "provider": "anthropic"},
    {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5",   "provider": "anthropic"},
]
OPENAI_MODELS = [
    {"id": "gpt-4o",       "label": "GPT-4o",       "provider": "openai"},
    {"id": "gpt-4o-mini",  "label": "GPT-4o mini",  "provider": "openai"},
    {"id": "gpt-4-turbo",  "label": "GPT-4 Turbo",  "provider": "openai"},
]


def available_providers() -> list[dict]:
    """Return list of available providers + their models based on set keys."""
    providers = []
    if ANTHROPIC_KEY:
        providers.append({"id": "anthropic", "label": "Anthropic", "models": ANTHROPIC_MODELS})
    if OPENAI_KEY:
        providers.append({"id": "openai", "label": "OpenAI", "models": OPENAI_MODELS})
    return providers


def default_provider() -> str:
    """Anthropic first if available, else OpenAI, else raise."""
    if ANTHROPIC_KEY:
        return "anthropic"
    if OPENAI_KEY:
        return "openai"
    raise RuntimeError(
        "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your .env file."
    )


def default_model(provider: str) -> str:
    return planner_model(provider)


def planner_model(provider: str) -> str:
    """Best reasoning model — used for planning (1 call)."""
    if provider == "anthropic":
        return "claude-sonnet-4-6"
    return "gpt-4o"


def executor_model(provider: str) -> str:
    """Fast cheap model — used for executor + reviewer (many tool-call loops)."""
    if provider == "anthropic":
        return "claude-haiku-4-5-20251001"
    return "gpt-4o-mini"


@lru_cache(maxsize=16)
def get_llm(provider: str, model: str, max_tokens: int = 4096):
    """Return a cached LangChain chat model for the given provider + model."""
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, max_tokens=max_tokens, api_key=OPENAI_KEY or None)
    else:
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, max_tokens=max_tokens, api_key=ANTHROPIC_KEY or None)
