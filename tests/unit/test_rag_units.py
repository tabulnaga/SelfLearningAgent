from pathlib import Path

from app.models import RetrievalScope
from app.rag.embed import HashingEmbedder
from app.rag.ingest.chunk import approx_tokens, chunk_document
from app.rag.ingest.parse import parse_file, parse_text
from app.rag.pg_store import PgKnowledgeStore, or_tsquery
from app.rag.rerank import NoopReranker
from app.rag.retriever import HybridRetriever, rrf_fuse

DOCS = Path(__file__).resolve().parents[2] / "data" / "docs"


def test_front_matter_parsed():
    d = parse_text("---\ntitle: T\nclassification: public\ndomain: payments\nversion: 2\n---\n# H\nbody", "x.md")
    assert (d.title, d.classification, d.domain, d.version, d.doc_id) == ("T", "public", "payments", "2", "x")
    assert d.body.startswith("# H")


def test_content_hash_changes_with_metadata():
    a = parse_text("---\nclassification: public\n---\nbody", "x.md")
    b = parse_text("---\nclassification: confidential\n---\nbody", "x.md")
    assert a.content_hash != b.content_hash  # reclassification must re-index


def test_chunks_keep_heading_path_and_budget():
    doc = parse_file(DOCS / "payment-pending-runbook.md")
    chunks = chunk_document(doc.body, max_tokens=120, overlap_tokens=20)
    assert len(chunks) > 3
    assert any(c.heading_path.endswith("Investigation order") for c in chunks)
    assert all(c.heading_path.startswith("Payment Pending Troubleshooting Runbook") for c in chunks)
    assert all(approx_tokens(c.content) <= 240 for c in chunks)  # a single long paragraph may exceed slightly


def test_all_sample_docs_parse():
    docs = [parse_file(p) for p in DOCS.glob("*.md")]
    assert {d.doc_id for d in docs} >= {"RB-PAY-001", "POL-PAY-004", "OPS-KAFKA-002", "CON-PSP-001"}
    assert next(d for d in docs if d.doc_id == "CON-PSP-001").classification == "confidential"


def test_or_tsquery_drops_stopwords_and_injection():
    q = or_tsquery("We have another Payment Pending incident. What should we investigate first?")
    assert q == "payment | pending | incident | investigate"
    assert or_tsquery("'; DROP TABLE x; --") == "drop | table"


def test_rrf_rewards_agreement():
    a = [{"chunk_id": "x"}, {"chunk_id": "y"}]
    b = [{"chunk_id": "y"}, {"chunk_id": "z"}]
    order = [row["chunk_id"] for row, _, _ in rrf_fuse(a, b)]
    assert order[0] == "y"


def test_scope_and_filters_are_parameterised():
    where, params = PgKnowledgeStore._where(RetrievalScope(tenant_id="t", classifications=["internal"]),
                                            {"domain": "payments", "evil; drop": "x"})
    assert "evil" not in where and params == ["t", ["internal"], "payments"]


class _Store:
    def __init__(self, vec_fail=False, kw_fail=False):
        self.vec_fail, self.kw_fail = vec_fail, kw_fail
        self.rows = [{"chunk_id": f"c{i}", "doc_id": "d", "doc_title": "T", "source": "s", "heading_path": "h",
                      "content": f"text {i}", "metadata": {}, "domain": "payments", "classification": "internal",
                      "score": 1.0 / (i + 1)} for i in range(5)]

    async def vector_search(self, embedding, scope, filters, limit):
        if self.vec_fail:
            raise RuntimeError("vector down")
        return self.rows[:limit]

    async def keyword_search(self, query, scope, filters, limit):
        if self.kw_fail:
            raise RuntimeError("fts down")
        return list(reversed(self.rows))[:limit]


SCOPE = RetrievalScope(tenant_id="t", classifications=["internal"])


async def test_retriever_returns_metadata_and_scores():
    r = await HybridRetriever(_Store(), HashingEmbedder(), NoopReranker(), top_k=3).retrieve("q", SCOPE)
    assert r.status == "ok" and len(r.evidence) == 3
    assert set(r.evidence[0].scores) == {"vector", "keyword", "fused", "rerank"}


async def test_retriever_degrades_per_arm():
    r = await HybridRetriever(_Store(vec_fail=True), HashingEmbedder(), NoopReranker()).retrieve("q", SCOPE)
    assert r.status == "partial" and r.evidence and "vector" in r.error
    r = await HybridRetriever(_Store(True, True), HashingEmbedder(), NoopReranker()).retrieve("q", SCOPE)
    assert r.status == "failed" and not r.evidence


async def test_rerank_floor_returns_no_weak_evidence():
    r = await HybridRetriever(_Store(), HashingEmbedder(), NoopReranker(), min_rerank_score=100).retrieve("q", SCOPE)
    assert r.status == "empty" and r.evidence == []
