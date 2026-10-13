"""Deterministic answer checks (spec §16; architecture §7). The checker is code, not the model that
wrote the answer (harness principle: maker != checker). Each failure message says how to fix it,
because it is fed back to the model for one regeneration (harness L10: agent-oriented errors)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models import DraftAnswer, Evidence, KnowledgePage, LearnedItem, ToolRecord

POLICY_LANGUAGE = re.compile(
    r"\b(must|must not|policy|required|requires|mandatory|prohibited|official(ly)?|procedure says)\b", re.I
)
RESOLUTION_CLAIM = re.compile(r"\b(resolved|fixed|restored|back to normal|completed successfully)\b", re.I)


@dataclass
class TurnContext:
    official: dict[str, Evidence] = field(default_factory=dict)     # D1 -> evidence
    learned: dict[str, LearnedItem] = field(default_factory=dict)   # M1 -> memory
    pages: dict[str, KnowledgePage] = field(default_factory=dict)   # P1 -> page
    tools: dict[str, ToolRecord] = field(default_factory=dict)      # T<id> -> tool record (this session)
    user_confirmed_resolution: bool = False

    def known(self, cite: str) -> bool:
        return cite in self.official or cite in self.learned or cite in self.pages or cite in self.tools


@dataclass
class CheckResult:
    name: str
    passed: bool
    problems: list[str] = field(default_factory=list)


def check_citations_valid(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    bad = sorted({c for claim in d.claims for c in claim.cites if not ctx.known(c)})
    return CheckResult("V1_citations_valid", not bad,
                       [f"Citations {bad} do not exist in this turn's context. Cite only IDs shown in the "
                        f"OFFICIAL KNOWLEDGE, LEARNED EXPERIENCE or TOOL RESULTS blocks."] if bad else [])


def check_grounded(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    problems = [f"Claim '{c.text[:60]}' is labelled {c.kind} but cites nothing. Add the supporting citation "
                f"or relabel it 'general'." for c in d.claims if c.kind in ("official", "learned", "tool")
                and not c.cites]
    if ctx.official and not any(c.kind == "official" for c in d.claims):
        problems.append("Official evidence was retrieved but no claim uses it. State what the official "
                        "documentation says, citing [D#].")
    return CheckResult("V2_grounded", not problems, problems)


def check_source_discipline(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    problems: list[str] = []
    for c in d.claims:
        if c.kind == "official" and any(not x.startswith("D") for x in c.cites):
            problems.append(f"Official claim '{c.text[:60]}' cites non-official sources "
                            f"{[x for x in c.cites if not x.startswith('D')]}. Official claims cite only [D#].")
        if c.kind == "learned" and any(x.startswith("D") for x in c.cites):
            problems.append(f"Learned claim '{c.text[:60]}' cites official documents. Split it into an "
                            f"official claim and a learned claim.")
        rests_on_learned = c.kind == "learned" or any(x[0] in "MP" for x in c.cites)
        if c.kind != "official" and rests_on_learned and POLICY_LANGUAGE.search(c.text):
            problems.append(f"Claim '{c.text[:60]}' presents learned experience with policy language. "
                            f"Learned experience is not policy: phrase it as what past cases showed.")
    return CheckResult("V3_source_discipline", not problems, problems)


def check_conflicts_surfaced(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    # Conflicts are rendered by code (renderer), so the check is that each conflict is non-empty and
    # that an official claim exists to state the winning position.
    problems = []
    if d.conflicts and not any(c.kind == "official" for c in d.claims):
        problems.append("A conflict was reported but no official claim states the official position. "
                        "Add the official position with its [D#] citation.")
    return CheckResult("V4_conflict_surfaced", not problems, problems)


def check_tool_truth(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    problems: list[str] = []
    for c in d.claims:
        if c.kind != "tool":
            continue
        cited = [ctx.tools[x] for x in c.cites if x in ctx.tools]
        if not cited:
            problems.append(f"Tool claim '{c.text[:60]}' must cite a tool result [T#].")
            continue
        if RESOLUTION_CLAIM.search(c.text) and not any(t.status == "success" for t in cited):
            problems.append(f"Claim '{c.text[:60]}' asserts success but the cited tool calls "
                            f"{[x for x in c.cites if x in ctx.tools]} did not succeed. Report the failure.")
    return CheckResult("V5_tool_truth", not problems, problems)


def check_no_false_resolution(d: DraftAnswer, ctx: TurnContext) -> CheckResult:
    remediated = any(t.side_effect == "write" and t.status == "success" for t in ctx.tools.values())
    claims_fixed = d.proposes_resolution or any(
        RESOLUTION_CLAIM.search(c.text) and c.kind in ("tool", "general") for c in d.claims)
    if claims_fixed and not (remediated or ctx.user_confirmed_resolution):
        return CheckResult("V6_no_false_resolution", False,
                           ["The answer claims the issue is resolved, but no remediation succeeded in this session "
                            "and the user has not confirmed. Do not claim resolution; state what is still pending."])
    return CheckResult("V6_no_false_resolution", True)


ALL_CHECKS = [check_citations_valid, check_grounded, check_source_discipline, check_conflicts_surfaced,
              check_tool_truth, check_no_false_resolution]
