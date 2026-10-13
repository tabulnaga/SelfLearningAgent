"""The final answer is composed by CODE from verified claims, so provenance cannot be lost in the
model's formatting: official claims, learned claims and tool results land in separate, labelled
sections, and every citation is expanded into a traceable source line (spec §4, §13)."""

from __future__ import annotations

from app.models import DraftAnswer
from app.verification.checks import TurnContext


def _cites(cites: list[str]) -> str:
    return " " + " ".join(f"[{c}]" for c in cites) if cites else ""


def render_answer(d: DraftAnswer, ctx: TurnContext, notes: list[str]) -> str:
    out: list[str] = []
    general = [c for c in d.claims if c.kind == "general"]
    official = [c for c in d.claims if c.kind == "official"]
    learned = [c for c in d.claims if c.kind == "learned"]
    tool = [c for c in d.claims if c.kind == "tool"]

    out.extend(c.text for c in general)
    if official:
        out.append("\nFrom official documentation:")
        out.extend(f"- {c.text}{_cites(c.cites)}" for c in official)
    if learned:
        out.append("\nFrom past cases (learned experience, not official policy):")
        out.extend(f"- {c.text}{_cites(c.cites)}" for c in learned)
    if tool:
        out.append("\nWhat the tools showed:")
        out.extend(f"- {c.text}{_cites(c.cites)}" for c in tool)
    if d.conflicts:
        out.append("\nConflict between past experience and official documentation "
                   "(official documentation takes precedence):")
        out.extend(f"- {x}" for x in d.conflicts)
    if d.next_step:
        out.append(f"\nNext step: {d.next_step}")
    for n in notes:
        out.append(f"\nNote: {n}")

    used = {c for claim in d.claims for c in claim.cites}
    sources = []
    for cid in sorted(used, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0)):
        if cid in ctx.official:
            e = ctx.official[cid]
            sources.append(f"[{cid}] OFFICIAL - {e.doc_title} > {e.heading_path} ({e.doc_id})")
        elif cid in ctx.learned:
            m = ctx.learned[cid]
            sources.append(f"[{cid}] LEARNED - {m.type} {m.memory_id}")
        elif cid in ctx.pages:
            sources.append(f"[{cid}] LEARNED - knowledge page '{ctx.pages[cid].name}'")
        elif cid in ctx.tools:
            t = ctx.tools[cid]
            sources.append(f"[{cid}] TOOL - {t.tool_name} ({t.status})")
    if sources:
        out.append("\nSources:\n" + "\n".join(sources))
    return "\n".join(out).strip()


def render_fallback(ctx: TurnContext, reason: str, notes: list[str]) -> str:
    """Controlled answer when reasoning or verification fails twice: official excerpts only, no synthesis."""
    out = [f"I could not produce a verified answer ({reason})."]
    if ctx.official:
        out.append("\nThese official documentation excerpts are the most relevant:")
        for cid, e in list(ctx.official.items())[:3]:
            snippet = e.content[:400].strip().replace("\n", " ")
            out.append(f"- [{cid}] {e.doc_title} > {e.heading_path}: {snippet}")
    else:
        out.append("No relevant official documentation was found.")
    for n in notes:
        out.append(f"\nNote: {n}")
    return "\n".join(out)
