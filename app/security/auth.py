"""Authentication: API key -> Principal. Extension point for OIDC/JWT later (spec §20)."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import get_settings
from app.models import Principal

_DEV_PRINCIPAL = Principal(subject="dev", tenant_id="default", roles=["agent_user", "operator", "admin"])


def _parse_keys(raw: str) -> dict[str, Principal]:
    """'key:tenant:role1|role2,key2:tenant2:role' -> {key: Principal}"""
    out: dict[str, Principal] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        key, tenant, roles = (entry.split(":") + ["", ""])[:3]
        out[key] = Principal(subject=f"key:{key[:4]}…", tenant_id=tenant or "default",
                             roles=[r for r in roles.split("|") if r])
    return out


def authenticate(x_api_key: str | None = Header(default=None)) -> Principal:
    settings = get_settings()
    if not settings.api_keys:
        if settings.app_env != "sim":
            raise HTTPException(500, "API_KEYS must be configured outside APP_ENV=sim")
        return _DEV_PRINCIPAL
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key")
    for key, principal in _parse_keys(settings.api_keys).items():
        if hmac.compare_digest(key, x_api_key):
            return principal
    raise HTTPException(401, "invalid API key")


def require_role(principal: Principal, role: str) -> None:
    if role not in principal.roles and "admin" not in principal.roles:
        raise HTTPException(403, f"role '{role}' required")
