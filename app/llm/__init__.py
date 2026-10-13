from __future__ import annotations

from app.config import Settings
from app.llm.interfaces import LLMClient


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "fake":
        from app.llm.fake import FakeLLM

        return FakeLLM()
    from app.llm.anthropic_client import AnthropicLLM

    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    return AnthropicLLM(settings.llm_model, key, settings.llm_max_tokens, settings.llm_timeout_s)
