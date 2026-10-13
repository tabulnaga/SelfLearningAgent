"""Deterministic LLM for tests and offline evals. Produces schema-valid outputs from simple rules over
the prompt, so the pipeline (context building, verification, rendering, memory writer) can be tested
without a provider. It is not a model of quality - live evals use the real provider."""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from app.llm.interfaces import LLMError, T, Usage
from app.models import CaseNarrative, Claim, DraftAnswer, ProposedTool, Understanding

_CITE = re.compile(r"\[(?P<id>[DMPT]\d+)\]")


def _ids(block: str, prefix: str) -> list[str]:
    return [m for m in dict.fromkeys(_CITE.findall(block)) if m.startswith(prefix)]


def _section(text: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return m.group(1) if m else ""


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_purposes: set[str] = set()
        self.overrides: dict[str, Callable[[str], object]] = {}

    async def complete_structured(self, *, system: str, user: str, schema: type[T], purpose: str):
        self.calls.append(purpose)
        if purpose in self.fail_purposes:
            raise LLMError(f"fake failure for {purpose}")
        if purpose in self.overrides:
            return schema.model_validate(self.overrides[purpose](user)), Usage(model="fake")
        if schema is Understanding:
            out = self._understand(user)
        elif schema is DraftAnswer:
            out = self._reason(user)
        elif schema is CaseNarrative:
            out = self._narrative(user)
        else:
            raise LLMError(f"FakeLLM has no rule for {schema.__name__}")
        return out, Usage(input_tokens=len(user) // 4, output_tokens=50, model="fake")

    @staticmethod
    def _understand(user: str) -> Understanding:
        msg = _section(user, "message").strip().lower()
        if re.fullmatch(r"(hi|hello|hey|thanks|thank you)[.! ]*", msg):
            return Understanding(intent="greeting", search_query=msg, is_smalltalk=True)
        if re.search(r"\b(resolved|fixed|works now|all good)\b", msg):
            return Understanding(intent="confirm_resolved", domain="payments", search_query=msg)
        if "escalat" in msg:
            return Understanding(intent="request_escalation", domain="payments", search_query=msg)
        domain = "payments" if re.search(r"payment|pending|consumer|kafka", msg) else "general"
        intent = "incident" if re.search(r"pending|stuck|incident|fail|down", msg) else "how_to"
        return Understanding(intent=intent, domain=domain, search_query=msg)

    @staticmethod
    def _reason(user: str) -> DraftAnswer:
        official = _section(user, "official_knowledge")
        learned = _section(user, "learned_experience")
        tools = _section(user, "tool_results")
        msg = _section(user, "message").lower()
        claims: list[Claim] = []
        d_ids, m_ids, t_ids = _ids(official, "D"), _ids(learned, "M") + _ids(learned, "P"), _ids(tools, "T")
        if d_ids:
            claims.append(Claim(text="The official runbook describes the troubleshooting procedure.",
                                kind="official", cites=d_ids[:2]))
        if m_ids:
            claims.append(Claim(text="Previous cases point to a pattern worth checking first.",
                                kind="learned", cites=m_ids[:2]))
        proposed: list[ProposedTool] = []
        resolved = False
        if t_ids:
            ok = "status=success" in tools
            claims.append(Claim(text="The tool results were recorded." if ok else "A tool call failed.",
                                kind="tool", cites=t_ids[-1:]))
            resolved = ok and "restart_payment_consumer" in tools and "status=success" in tools
        elif "pending" in msg and "<tools_available>" in user:
            proposed.append(ProposedTool(name="check_consumer_status",
                                         arguments_json=json.dumps({"consumer": "payment-events"})))
        if not claims:
            claims.append(Claim(text="I could not find this in the documentation.", kind="general"))
        return DraftAnswer(claims=claims, proposed_tools=proposed, proposes_resolution=resolved,
                           next_step="Confirm whether the issue is resolved.")

    @staticmethod
    def _narrative(user: str) -> CaseNarrative:
        conv = _section(user, "conversation").lower()
        issue = "Payments stuck in Pending status" if "pending" in conv else "Support request"
        root = "Payment event consumer stalled" if "stalled" in user.lower() else None
        lessons = ["Check the payment consumer status before retrying payments."] if root else []
        return CaseNarrative(issue=issue, root_cause=root, lessons=lessons, confidence=0.8 if root else 0.4)
