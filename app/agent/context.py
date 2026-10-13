"""Grounded context construction (spec §4): two separate, labelled evidence blocks - never merged -
with short stable citation IDs. Retrieved text is fenced as data."""

from __future__ import annotations

import json

from app.models import Evidence, KnowledgePage, LearnedItem, ToolRecord
from app.verification.checks import TurnContext

MAX_PAGE_CHARS = 1500


def assign_ids(
    evidence: list[Evidence], learned: list[LearnedItem], pages: list[KnowledgePage], tools: list[ToolRecord]
) -> TurnContext:
    ctx = TurnContext()
    for i, e in enumerate(evidence, 1):
        e.cite_id = f"D{i}"
        ctx.official[e.cite_id] = e
    for i, m in enumerate(learned, 1):
        m.cite_id = f"M{i}"
        ctx.learned[m.cite_id] = m
    for i, p in enumerate(pages, 1):
        p.cite_id = f"P{i}"
        ctx.pages[p.cite_id] = p
    for t in tools:
        ctx.tools[f"T{t.tool_exec_id}"] = t
    return ctx


def _fence(text: str) -> str:
    return text.replace("</", "<\\/")  # keep retrieved text from closing our blocks


def render_official(ctx: TurnContext, status: str) -> str:
    if not ctx.official:
        note = {"failed": "retrieval unavailable", "empty": "no relevant documentation found"}.get(status, "none")
        return f'<official_knowledge status="{status}">({note})</official_knowledge>'
    parts = [
        f"[{cid}] {e.doc_title} > {e.heading_path} (source: {e.source})\n{_fence(e.content)}"
        for cid, e in ctx.official.items()
    ]
    return f'<official_knowledge status="{status}">\n' + "\n\n".join(parts) + "\n</official_knowledge>"


def render_learned(ctx: TurnContext, status: str) -> str:
    if not ctx.learned and not ctx.pages:
        note = {"degraded": "memory temporarily unavailable", "disabled": "memory disabled"}.get(status, "none")
        return f'<learned_experience status="{status}">({note})</learned_experience>'
    parts = []
    for cid, m in ctx.learned.items():
        support = f", consolidated from {m.proof_count} facts" if m.proof_count else ""
        parts.append(f"[{cid}] ({m.type}{support}) {_fence(m.text)}")
    for cid, p in ctx.pages.items():
        parts.append(f"[{cid}] (standing knowledge page: {p.name})\n{_fence(p.content[:MAX_PAGE_CHARS])}")
    return f'<learned_experience status="{status}">\n' + "\n".join(parts) + "\n</learned_experience>"


def render_tools(ctx: TurnContext) -> str:
    if not ctx.tools:
        return "<tool_results>(none yet in this session)</tool_results>"
    lines = []
    for cid, t in ctx.tools.items():
        body = json.dumps(t.output, default=str) if t.output is not None else t.error
        lines.append(f"[{cid}] {t.tool_name}({json.dumps(t.input)}) status={t.status}: {_fence(str(body))}")
    return "<tool_results>\n" + "\n".join(lines) + "\n</tool_results>"


def render_history(messages: list[dict], limit: int = 10) -> str:
    recent = messages[-limit:]
    return "<conversation>\n" + "\n".join(
        f"{m['role']}: {_fence(m['content'][:800])}" for m in recent) + "\n</conversation>"
