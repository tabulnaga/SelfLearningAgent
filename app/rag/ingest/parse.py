"""Document parsing. v1 handles Markdown/plain text with an optional front-matter block:

    ---
    title: Payment Pending Troubleshooting Runbook
    classification: internal
    domain: payments
    version: 3.2
    ---
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    source: str
    body: str
    classification: str = "internal"
    domain: str | None = None
    version: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        meta = f"{self.title}|{self.classification}|{self.domain}|{self.version}"
        return hashlib.sha256((meta + "\n" + self.body).encode("utf-8")).hexdigest()


def parse_text(text: str, source: str, doc_id: str | None = None) -> ParsedDocument:
    meta: dict[str, str] = {}
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        text = text[m.end():]
    title = meta.pop("title", None)
    if not title:
        h1 = re.search(r"^#\s+(.+)$", text, re.M)
        title = h1.group(1).strip() if h1 else Path(source).stem
    return ParsedDocument(
        doc_id=doc_id or meta.pop("doc_id", None) or Path(source).stem,
        title=title,
        source=source,
        body=text.strip(),
        classification=meta.pop("classification", "internal"),
        domain=meta.pop("domain", None),
        version=meta.pop("version", None),
        metadata=meta,
    )


def parse_file(path: Path) -> ParsedDocument:
    return parse_text(path.read_text(encoding="utf-8"), source=path.name)
