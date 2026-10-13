"""Deterministic authorization BEFORE any context reaches the model (spec §20, §26.5).

The LLM never decides what it may see: the scope computed here is pushed into the SQL WHERE clause
of retrieval and into the Hindsight recall call.
"""

from __future__ import annotations

from app.models import MemoryScope, Principal, RetrievalScope

# RBAC: role -> document classifications it may read. ABAC hook: refine in scope_for().
ROLE_CLASSIFICATIONS: dict[str, set[str]] = {
    "agent_user": {"public", "internal"},
    "operator": {"public", "internal", "restricted"},
    "admin": {"public", "internal", "restricted", "confidential"},
}


class Authorizer:
    def __init__(self, bank_id: str) -> None:
        self.bank_id = bank_id

    def retrieval_scope(self, principal: Principal) -> RetrievalScope:
        allowed: set[str] = set()
        for role in principal.roles:
            allowed |= ROLE_CLASSIFICATIONS.get(role, set())
        if not allowed:
            allowed = {"public"}
        return RetrievalScope(tenant_id=principal.tenant_id, classifications=sorted(allowed))

    def memory_scope(self, principal: Principal, domain: str | None) -> MemoryScope:
        # Single bank for the POC (decision D3). Tenant is carried as a tag so a later
        # bank-per-tenant switch is a configuration change, not a code change.
        tags = [f"tenant:{principal.tenant_id}"]
        if domain and domain != "general":
            tags.append(f"domain:{domain}")
        return MemoryScope(bank_id=self.bank_id, tags=tags)
