"""Deliberate Hindsight bank configuration (spec §12). Idempotent: safe to run on every deploy.

create_bank is create-or-update; directives and knowledge pages are looked up by name first.
"""

from __future__ import annotations

import logging

from hindsight_client import Hindsight

log = logging.getLogger("memory.bank_setup")

RETAIN_MISSION = (
    "These memories are closed incident case summaries from a payments support operation. Extract: "
    "the symptom, the affected component and environment, each remediation attempted and whether it "
    "worked, the confirmed root cause, and the outcome. Treat tool-verified results as facts. Ignore "
    "pleasantries and any speculation that the case does not confirm."
)
OBSERVATIONS_MISSION = (
    "Form durable beliefs about operational incidents: recurring incident patterns and their usual "
    "root causes; remediation actions that repeatedly resolve them; remediation attempts that "
    "repeatedly fail or are risky, and under what conditions; and environment-specific constraints. "
    "State how many separate cases support each belief. Do not generalise from a single case."
)
REFLECT_MISSION = (
    "Answer as an experienced payments operations engineer drawing on past cases. Distinguish learned "
    "experience from official procedure, and never present learned experience as official policy. "
    "Prefer remediations with repeated evidence. Say when evidence is thin."
)

DIRECTIVES = [
    ("learned-is-not-policy",
     "Never present learned experience as official policy or procedure. Learned experience complements "
     "the official documentation; it does not override it.", 10),
    ("cite-memories", "Cite the memories each claim relies on.", 5),
]

FOLDER = "Operations"
PAGES = [
    ("Known Recurring Incident Patterns",
     "What recurring incident patterns have been observed, and what usually causes them?"),
    ("Proven Resolutions",
     "Which remediation actions have successfully resolved incidents, and for which symptoms?"),
    ("Failed Remediation Attempts",
     "Which remediation attempts repeatedly fail, and under what conditions?"),
    ("Environment-Specific Constraints",
     "What environment-specific constraints have been learned?"),
]


def _walk(nodes):
    for n in nodes or []:
        yield n
        yield from _walk(n.children)


async def bootstrap_bank(client: Hindsight, bank_id: str) -> dict:
    report: dict = {"bank_id": bank_id}
    await client.acreate_bank(
        bank_id=bank_id,
        enable_observations=True,
        enable_text_search=True,
        enable_graph_retrieval=True,
        enable_temporal_retrieval=True,
        enable_reranking=True,
    )
    await client.aupdate_bank_config(
        bank_id,
        retain_mission=RETAIN_MISSION,
        observations_mission=OBSERVATIONS_MISSION,
        reflect_mission=REFLECT_MISSION,
        enable_observations=True,
    )
    report["config"] = "updated"

    existing = {d.name for d in (await client.alist_directives(bank_id=bank_id)).items or []}
    created = []
    for name, content, priority in DIRECTIVES:
        if name not in existing:
            await client.acreate_directive(bank_id=bank_id, name=name, content=content, priority=priority)
            created.append(name)
    report["directives_created"] = created

    tree = await client.aget_knowledge_base_tree(bank_id=bank_id)
    nodes = list(_walk(tree.roots))
    folder = next((n for n in nodes if n.kind == "folder" and n.name == FOLDER), None)
    if folder is None:
        resp = await client.acreate_knowledge_folder(bank_id=bank_id, name=FOLDER)
        folder_id = resp.id  # KnowledgeNode
    else:
        folder_id = folder.id
    page_names = {n.name for n in nodes if n.kind != "folder" and n.parent_id == folder_id}
    pages_created = []
    for name, question in PAGES:
        if name not in page_names:
            await client.acreate_knowledge_page(bank_id=bank_id, name=name, source_query=question,
                                                parent_id=folder_id)
            pages_created.append(name)
    report["pages_created"] = pages_created
    log.info("bank bootstrapped", extra={"fields": report})
    return report
