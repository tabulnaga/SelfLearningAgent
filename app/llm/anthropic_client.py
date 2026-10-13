"""Anthropic adapter. Structured outputs via messages.parse(output_format=...).

Claude Opus 5.5 notes that shape this file: forced tool_choice and sampling params are rejected, and
thinking is always on with effort defaulting to 'medium' - so we set effort explicitly per purpose
and get JSON through structured outputs instead of a forced tool call.
"""

from __future__ import annotations

import anthropic

from app.llm.interfaces import LLMError, T, Usage

# Per-call effort: classification-like calls are cheap; answer synthesis and case writing get more.
_EFFORT = {"understand": "low", "reason": "medium", "case_narrative": "medium"}


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None, max_tokens: int, timeout_s: float) -> None:
        self.model = model
        self.max_tokens = max_tokens
        # api_key=None lets the SDK resolve credentials itself (env var or `ant auth login` profile).
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def complete_structured(
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> tuple[T, Usage]:
        try:
            # Server-side refusal fallback: if a safety classifier declines, the API reruns the same
            # request on a model chosen by refusal category inside this one call.
            response = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": _EFFORT.get(purpose, "medium")},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"anthropic {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e

        if response.stop_reason == "refusal":
            raise LLMError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("output truncated at max_tokens")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("no parsed output")
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=self.model,
        )
        return parsed, usage
