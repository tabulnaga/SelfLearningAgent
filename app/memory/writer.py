"""Memory Writer (spec §7, §9, §11; architecture §6.3) - the ONLY path to Hindsight retain().

What it retains is a validated case outcome, not a transcript. Facts about what was done come from
the persisted tool log (deterministic); the LLM writes only the narrative fields (issue, root cause,
lessons, confidence). Retention criteria R1-R5 are explicit and every skip records its reason.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from app.agent.context import render_history
from app.agent.prompts import NARRATIVE_SYSTEM
from app.llm.interfaces import LLMClient
from app.memory.interfaces import RetainRequest
from app.models import CaseNarrative, CaseRecord, ToolRecord

SCHEMA_VERSION = "case.v1"
OPERATION_NS = uuid.UUID("6f1c2b4e-9a57-4c1e-8b0f-5a3d2e7c9b10")
TRIVIAL_INTENTS = {"greeting", "smalltalk", "faq_only", "confirm_resolved"}
CONTEXT = ("Closed incident case summary written by the support operations agent after the case closed. "
           "It describes what operators observed, what was tried, and what happened.")


@dataclass
class RetentionDecision:
    retain: bool
    reason: str


def evaluate_retention(session: dict, intents: list[str], tools: list[ToolRecord], has_operator_input: bool,
                       remediation_tools: set[str]) -> RetentionDecision:
    """R1-R3 (before any LLM cost). R4 is the job's uniqueness + content hash; R5 runs after extraction."""
    if not intents or set(intents) <= TRIVIAL_INTENTS:
        return RetentionDecision(False, "R1: no substantive request (greeting/smalltalk/FAQ only)")
    if not tools and not has_operator_input:
        return RetentionDecision(False, "R2: no tool evidence and no operator outcome - nothing verified to learn")
    reason = session["closure_reason"]
    if reason == "TIMED_OUT":
        ok = any(t.tool_name in remediation_tools and t.status == "success" for t in tools)
        if not ok:
            return RetentionDecision(False, "R3: timed out without a verified remediation - no outcome to learn")
    elif reason not in ("RESOLVED", "ESCALATED"):
        return RetentionDecision(False, f"R3: closure reason {reason!r} is not retainable")
    return RetentionDecision(True, "R1-R3 passed")


def _describe(t: ToolRecord) -> str:
    args = ", ".join(f"{k}={v}" for k, v in t.input.items())
    if t.status == "success":
        return f"{t.tool_name}({args}) succeeded: {json.dumps(t.output, default=str)}"
    return f"{t.tool_name}({args}) {'was denied' if t.status == 'denied' else 'failed'}: {t.error}"


def derive_actions(tools: list[ToolRecord], remediation_tools: set[str]) -> dict:
    remediations = [t for t in tools if t.tool_name in remediation_tools]
    successful = [t for t in remediations if t.status == "success"]
    return {
        "diagnostics": [_describe(t) for t in tools if t.tool_name not in remediation_tools],
        "actions_attempted": [_describe(t) for t in remediations],
        "failed_actions": [_describe(t) for t in remediations if t.status != "success"],
        "successful_action": _describe(successful[-1]) if successful else None,
    }


def outcome_for(closure_reason: str, successful_action: str | None) -> str:
    if closure_reason == "RESOLVED":
        return "resolved"
    if closure_reason == "ESCALATED":
        return "escalated"
    return "resolved" if successful_action else "unresolved"


async def write_narrative(llm: LLMClient, messages: list[dict], actions: dict, outcome_note: str | None,
                          outcome: str) -> CaseNarrative:
    user = "\n\n".join([
        render_history(messages, limit=30),
        "<tool_log>\n" + "\n".join(actions["diagnostics"] + actions["actions_attempted"]) + "\n</tool_log>",
        f"<outcome>{outcome}</outcome>",
        f"<operator_note>{outcome_note or ''}</operator_note>",
    ])
    narrative, _ = await llm.complete_structured(system=NARRATIVE_SYSTEM, user=user, schema=CaseNarrative,
                                                 purpose="case_narrative")
    return narrative


def validate_record(record: CaseRecord, min_confidence: float, user_or_operator_confirmed: bool) -> str | None:
    """R5. Returns a rejection reason, or None if the record may be retained."""
    if not record.issue.strip():
        return "R5: empty issue"
    if record.confidence < min_confidence:
        return f"R5: confidence {record.confidence:.2f} below {min_confidence:.2f}"
    if record.outcome == "resolved" and not (record.successful_action or user_or_operator_confirmed):
        return "R5: outcome 'resolved' without a successful remediation or confirmation"
    if record.successful_action and record.successful_action in record.failed_actions:
        return "R5: an action is recorded as both successful and failed"
    return None


def render_content(record: CaseRecord, diagnostics: list[str]) -> str:
    """Prose first (Hindsight extracts better from prose), structured lines after."""
    lines = [
        f"Incident case closed on {record.closed_at[:10]} as {record.closure_reason.lower()} "
        f"in the {record.domain} domain.",
        f"Issue: {record.issue}",
    ]
    if diagnostics:
        lines.append("Diagnostics: " + "; ".join(diagnostics))
    if record.actions_attempted:
        lines.append("Remediation attempts, in order: " + "; ".join(
            f"({i}) {a}" for i, a in enumerate(record.actions_attempted, 1)))
    if record.failed_actions:
        lines.append("Did not work: " + "; ".join(record.failed_actions))
    if record.successful_action:
        lines.append(f"What resolved it: {record.successful_action}")
    lines.append(f"Root cause: {record.root_cause or 'not established'}")
    lines.append(f"Outcome: {record.outcome}")
    if record.lessons:
        lines.append("Lessons: " + " ".join(record.lessons))
    return "\n".join(lines)


def build_retain_request(record: CaseRecord, content: str, bank_id: str, env: str, retain_async: bool,
                         attempt_salt: int = 0) -> tuple[RetainRequest, str]:
    """Idempotency (reference-analysis §1.2): document_id per case makes retain an upsert; the
    operation_id is derived from content so a retried submission of the same content is recognised.
    Per-case IDs never go in tags (§1.3) - only in document_id/metadata."""
    document_id = f"case-{record.session_id}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    operation_id = str(uuid.uuid5(OPERATION_NS, f"{document_id}:{content_hash}:{attempt_salt}"))
    tenant_tag = f"tenant:{record.tenant_id}"
    domain_tag = f"domain:{record.domain}"
    req = RetainRequest(
        bank_id=bank_id,
        document_id=document_id,
        content=content,
        context=CONTEXT,
        tags=["source:case", domain_tag, f"outcome:{record.outcome}", f"env:{env}", tenant_tag],
        metadata={
            "schema_version": SCHEMA_VERSION,
            "session_id": record.session_id,
            "conversation_id": record.conversation_id,
            "closure_reason": record.closure_reason,
            "outcome": record.outcome,
            "confidence": f"{record.confidence:.2f}",
            "closed_at": record.closed_at,
        },
        timestamp=record.closed_at,
        # consolidate across cases of the same tenant+domain, regardless of outcome tag
        observation_scopes=[[tenant_tag, domain_tag]],
        operation_id=operation_id,
        retain_async=retain_async,
    )
    return req, content_hash
