from app.agent.renderer import render_answer
from app.models import Claim, DraftAnswer, Evidence, LearnedItem, ToolRecord
from app.verification.checks import TurnContext
from app.verification.verifier import Verifier


def ctx(with_tools: list[ToolRecord] | None = None) -> TurnContext:
    c = TurnContext()
    c.official["D1"] = Evidence(cite_id="D1", doc_id="RB-PAY-001", doc_title="Runbook", chunk_id="RB#1",
                                source="rb.md", heading_path="Runbook > Investigation order", content="...")
    c.learned["M1"] = LearnedItem(cite_id="M1", memory_id="obs-1", type="observation", text="...")
    for t in with_tools or []:
        c.tools[f"T{t.tool_exec_id}"] = t
    return c


def tool(i, name, status, side="write"):
    return ToolRecord(tool_exec_id=i, tool_name=name, side_effect=side, input={}, status=status,
                      output={"ok": True} if status == "success" else None,
                      error=None if status == "success" else "boom")


def failed(draft, c):
    return Verifier().verify(draft, c).summary()["failed_checks"]


GOOD = DraftAnswer(claims=[
    Claim(text="Check the PSP status first.", kind="official", cites=["D1"]),
    Claim(text="In previous cases a stalled consumer was the cause.", kind="learned", cites=["M1"]),
])


def test_good_answer_passes():
    assert failed(GOOD, ctx()) == []


def test_invented_citation_fails():
    d = DraftAnswer(claims=[Claim(text="x", kind="official", cites=["D9"])])
    assert "V1_citations_valid" in failed(d, ctx())


def test_learned_presented_as_policy_fails():
    d = DraftAnswer(claims=[Claim(text="x", kind="official", cites=["D1"]),
                            Claim(text="You must restart the consumer.", kind="learned", cites=["M1"])])
    assert "V3_source_discipline" in failed(d, ctx())


def test_official_claim_citing_memory_fails():
    d = DraftAnswer(claims=[Claim(text="x", kind="official", cites=["M1"])])
    assert "V3_source_discipline" in failed(d, ctx())


def test_ignoring_official_evidence_fails():
    d = DraftAnswer(claims=[Claim(text="past cases say restart", kind="learned", cites=["M1"])])
    assert "V2_grounded" in failed(d, ctx())


def test_claiming_success_of_failed_tool_fails():
    c = ctx([tool(7, "restart_consumer", "error")])
    d = DraftAnswer(claims=[Claim(text="x", kind="official", cites=["D1"]),
                            Claim(text="The consumer restart resolved it.", kind="tool", cites=["T7"])])
    assert "V5_tool_truth" in failed(d, c)


def test_false_resolution_fails_without_remediation():
    d = GOOD.model_copy(update={"proposes_resolution": True})
    assert "V6_no_false_resolution" in failed(d, ctx([tool(3, "check_consumer_status", "success", "read")]))
    assert "V6_no_false_resolution" not in failed(d, ctx([tool(4, "restart_consumer", "success")]))


def test_conflict_requires_official_position():
    d = DraftAnswer(claims=[Claim(text="past cases retried", kind="learned", cites=["M1"])],
                    conflicts=["Past cases retried PENDING payments; policy forbids it."])
    assert "V4_conflict_surfaced" in failed(d, ctx())


def test_renderer_separates_provenance():
    out = render_answer(GOOD.model_copy(update={"conflicts": ["c"]}), ctx(), notes=["memory note"])
    official_at = out.index("From official documentation")
    learned_at = out.index("From past cases (learned experience, not official policy)")
    assert official_at < learned_at
    assert "[D1] OFFICIAL - Runbook" in out and "[M1] LEARNED - observation obs-1" in out
    assert "official documentation takes precedence" in out
