from app.llm.fake import FakeLLM
from app.memory import writer
from app.models import CaseRecord, ToolRecord
from app.tools.payments_sim import REMEDIATION_TOOLS


def t(i, name, status, side="write", **out):
    return ToolRecord(tool_exec_id=i, tool_name=name, side_effect=side, input={"consumer": "payment-events"},
                      status=status, output=out or None, error=None if status == "success" else "rejected")


CASE_TOOLS = [
    t(1, "check_consumer_status", "success", "read", state="stalled", lag=1840),
    t(2, "retry_payment", "error"),
    t(3, "restart_consumer", "success", pending_payments_completed=3),
]


def session(reason):
    return {"closure_reason": reason}


def decide(reason, intents=("incident",), tools=CASE_TOOLS, operator=False):
    return writer.evaluate_retention(session(reason), list(intents), list(tools), operator, REMEDIATION_TOOLS)


def test_retention_criteria():
    assert decide("RESOLVED").retain
    assert decide("ESCALATED").retain
    assert not decide("RESOLVED", intents=("greeting", "confirm_resolved")).retain  # R1
    assert not decide("RESOLVED", tools=[]).retain                                  # R2
    assert decide("RESOLVED", tools=[], operator=True).retain                       # R2 via operator note
    assert decide("TIMED_OUT").retain                                               # R3 with remediation
    assert not decide("TIMED_OUT", tools=CASE_TOOLS[:2]).retain                     # R3 without
    assert decide("TIMED_OUT", tools=CASE_TOOLS[:2]).reason.startswith("R3")


def test_actions_come_from_tool_log():
    a = writer.derive_actions(CASE_TOOLS, REMEDIATION_TOOLS)
    assert len(a["actions_attempted"]) == 2 and len(a["failed_actions"]) == 1
    assert a["successful_action"].startswith("restart_consumer")
    assert a["diagnostics"][0].startswith("check_consumer_status")


def record(**kw):
    base = dict(session_id="s1", conversation_id="c1", tenant_id="default", domain="payments",
                closure_reason="RESOLVED", closed_at="2026-10-04T10:00:00+00:00", issue="Payments stuck PENDING",
                outcome="resolved", confidence=0.8, successful_action="restart_consumer(...) succeeded")
    return CaseRecord(**{**base, **kw})


def test_validation_rejects_unsupported_records():
    assert writer.validate_record(record(), 0.5, True) is None
    assert writer.validate_record(record(confidence=0.2), 0.5, True).startswith("R5")
    assert writer.validate_record(record(successful_action=None), 0.5, False).startswith("R5")


def test_retain_request_is_idempotent_and_tags_stay_stable():
    r = record()
    content = writer.render_content(r, ["check_consumer_status(...) succeeded"])
    req1, h1 = writer.build_retain_request(r, content, "bank", "sim", True)
    req2, h2 = writer.build_retain_request(r, content, "bank", "sim", True)
    assert (req1.document_id, req1.operation_id, h1) == (req2.document_id, req2.operation_id, h2)
    assert req1.document_id == "case-s1"
    # per-case ids must never be tags (they would split consolidation - reference-analysis §1.3)
    assert not any("s1" in tag or "c1" in tag for tag in req1.tags)
    assert req1.observation_scopes == [["tenant:default", "domain:payments"]]
    other, _ = writer.build_retain_request(record(session_id="s2", conversation_id="c2"), content, "bank", "sim", True)
    assert set(other.tags) == set(req1.tags)  # two cases of the same kind consolidate together
    salted, _ = writer.build_retain_request(r, content, "bank", "sim", True, attempt_salt=1)
    assert salted.operation_id != req1.operation_id and salted.document_id == req1.document_id


def test_content_is_prose_with_structured_lines():
    c = writer.render_content(record(failed_actions=["retry_payment failed"]), [])
    assert c.splitlines()[0].startswith("Incident case closed on 2026-10-04 as resolved")
    assert "Did not work: retry_payment failed" in c and "What resolved it:" in c


async def test_narrative_via_llm():
    msgs = [{"role": "user", "content": "Payments are stuck in pending"}]
    actions = writer.derive_actions(CASE_TOOLS, REMEDIATION_TOOLS)
    n = await writer.write_narrative(FakeLLM(), msgs, actions, None, "resolved")
    assert "Pending" in n.issue and n.root_cause and n.confidence >= 0.5
