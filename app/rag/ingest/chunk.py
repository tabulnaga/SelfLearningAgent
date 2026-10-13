"""Heading-aware chunking. Splits on Markdown headings first (a procedure step should not be cut
across sections), then packs paragraphs up to a token budget with overlap. Token counts are
approximated (words * 1.3) - adequate for chunk sizing, and dependency-free."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class Chunk:
    ord: int
    heading_path: str
    content: str


def approx_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3) + 1


def _sections(body: str) -> list[tuple[str, str]]:
    path: list[str] = []
    current: list[str] = []
    out: list[tuple[str, str]] = []

    def flush():
        text = "\n".join(current).strip()
        if text:
            out.append((" > ".join(path), text))
        current.clear()

    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2)]
        else:
            current.append(line)
    flush()
    return out


def chunk_document(body: str, max_tokens: int = 400, overlap_tokens: int = 60) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading_path, text in _sections(body):
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        buf: list[str] = []
        for para in paras:
            if buf and approx_tokens("\n\n".join(buf + [para])) > max_tokens:
                chunks.append(Chunk(len(chunks), heading_path, "\n\n".join(buf)))
                tail: list[str] = []
                for p in reversed(buf):
                    if approx_tokens("\n\n".join([p] + tail)) > overlap_tokens:
                        break
                    tail.insert(0, p)
                buf = tail
            buf.append(para)
        if buf:
            chunks.append(Chunk(len(chunks), heading_path, "\n\n".join(buf)))
    return chunks
