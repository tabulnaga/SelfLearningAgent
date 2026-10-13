"""Typed tool interface (spec §13 'Tools'). Each tool declares input/output models and whether it has a
side effect. The LLM may only PROPOSE calls; this module validates them, the ToolPolicy decides, and
every execution - success, error or denial - is persisted and citable as [T:<id>]."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.models import ProposedTool, ToolRecord


class ToolError(Exception):
    """A tool ran and failed. The message is shown to the model and the user as-is."""


@dataclass
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    side_effect: Literal["read", "write"]
    run: Callable[[BaseModel], Awaitable[dict[str, Any]]]

    def signature(self) -> str:
        fields = ", ".join(f"{k}: {v.annotation.__name__ if hasattr(v.annotation, '__name__') else v.annotation}"
                           for k, v in self.input_model.model_fields.items())
        return f"- {self.name}({fields}) [{self.side_effect}] - {self.description}"


class ToolPolicy:
    """Deterministic gate outside the LLM (spec §20, decision D2): read tools always run; write tools
    run only when allowed by configuration (auto-approved in APP_ENV=sim, operator approval otherwise)."""

    def __init__(self, write_allowed: bool) -> None:
        self.write_allowed = write_allowed

    def decide(self, tool: Tool) -> tuple[bool, str]:
        if tool.side_effect == "read":
            return True, "read tools are always allowed"
        if self.write_allowed:
            return True, "write tools auto-approved (simulation environment)"
        return False, "write tool requires operator approval outside the simulation environment"


class ToolRegistry:
    def __init__(self, tools: list[Tool], policy: ToolPolicy) -> None:
        self.tools = {t.name: t for t in tools}
        self.policy = policy

    def describe(self) -> str:
        return "\n".join(t.signature() for t in self.tools.values())

    async def execute(
        self, proposal: ProposedTool, record: Callable[..., Awaitable[ToolRecord]]
    ) -> ToolRecord:
        """Validate -> policy -> run -> persist. Never raises for tool-level problems: the outcome is
        always a persisted ToolRecord with an honest status."""
        tool = self.tools.get(proposal.name)
        try:
            args = json.loads(proposal.arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return await record(tool_name=proposal.name, side_effect=tool.side_effect if tool else "read",
                                input={"raw": proposal.arguments_json}, output=None, status="error",
                                error=f"invalid arguments: {e}", duration_ms=0)
        if tool is None:
            return await record(tool_name=proposal.name, side_effect="read", input=args, output=None,
                                status="error", error=f"unknown tool '{proposal.name}'", duration_ms=0)
        try:
            parsed = tool.input_model.model_validate(args)
        except ValidationError as e:
            return await record(tool_name=tool.name, side_effect=tool.side_effect, input=args, output=None,
                                status="error", error=f"invalid arguments: {e.errors(include_url=False)}",
                                duration_ms=0)
        allowed, why = self.policy.decide(tool)
        if not allowed:
            return await record(tool_name=tool.name, side_effect=tool.side_effect, input=args, output=None,
                                status="denied", error=why, duration_ms=0)
        t0 = time.perf_counter()
        try:
            output = await tool.run(parsed)
            status, error = "success", None
        except ToolError as e:
            output, status, error = None, "error", str(e)
        except Exception as e:  # unexpected failure: still recorded, never reported as success
            output, status, error = None, "error", f"{type(e).__name__}: {e}"
        return await record(tool_name=tool.name, side_effect=tool.side_effect, input=args, output=output,
                            status=status, error=error, duration_ms=int((time.perf_counter() - t0) * 1000))
