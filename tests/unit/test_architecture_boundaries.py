"""Executable architecture rules (architecture §1): the three sources of truth stay separate and the
agent depends on interfaces, not drivers. A failure message says which rule broke and why."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

RULES = [
    ("rag", "app.memory", "AUTHORITATIVE KNOWLEDGE (rag/) must not depend on LEARNED EXPERIENCE (memory/)"),
    ("rag", "hindsight_client", "Hindsight is not the RAG store (spec §26.1)"),
    ("memory", "app.rag", "LEARNED EXPERIENCE (memory/) must not depend on AUTHORITATIVE KNOWLEDGE (rag/)"),
    ("agent", "psycopg", "the orchestrator talks to stores through their classes, not the DB driver"),
    ("agent", "hindsight_client", "the orchestrator uses the LearnedMemory interface, not the Hindsight client"),
    ("agent", "anthropic", "the orchestrator uses the LLMClient interface, not a provider SDK"),
    ("verification", "app.llm", "the checker must not be an LLM call (maker != checker)"),
]


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_boundaries():
    violations = []
    for package, forbidden, why in RULES:
        for f in (APP / package).rglob("*.py"):
            bad = [m for m in imports_of(f) if m == forbidden or m.startswith(forbidden + ".")]
            if bad:
                violations.append(f"{f.relative_to(APP.parent)} imports {bad}: {why}")
    assert not violations, "\n".join(violations)


def test_only_memory_writer_path_retains():
    """Spec §7: retain happens only through the Memory Writer, never per message."""
    offenders = [str(f.relative_to(APP.parent)) for f in APP.rglob("*.py")
                 if "retain_case(" in f.read_text(encoding="utf-8")
                 and f.name not in {"interfaces.py", "hindsight_adapter.py"}]
    assert offenders == [], f"retain_case called outside workers/memory_jobs.py: {offenders}"
