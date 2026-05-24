"""Worker-only auth dependency for the cockpit_bridge.

Scope: SOLELY for the worker calling /think-loop/decisions/{id}/start and
/complete. Mounted ONLY on those routes. NEVER a fallback for user-facing auth.

Why a dedicated dependency and token: in cf_access mode, LOCAL_AUTH_TOKEN must
not bypass CF Access for user routes. A separate ``COCKPIT_WORKER_TOKEN`` plus
a separate dependency keeps the surface narrow.

Auth order inside this dep:
1. If valid user auth (CF Access JWT in cf_access mode, bearer in local mode),
   return as user.
2. Else, if X-Cockpit-Worker-Token matches settings.cockpit_worker_token,
   return synthetic local-user actor.
3. Else 401.
"""

from __future__ import annotations

from hmac import compare_digest

from fastapi import Depends, Header, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.auth import (
    AuthContext,
    _get_or_create_local_user,
    get_auth_context_optional,
)
from app.core.config import settings
from app.db.session import get_session


async def get_user_or_worker_auth_context(
    x_cockpit_worker_token: str | None = Header(default=None, alias="X-Cockpit-Worker-Token"),
    session: AsyncSession = Depends(get_session),
    user_auth: AuthContext | None = Depends(get_auth_context_optional),
) -> AuthContext:
    if user_auth is not None:
        return user_auth

    expected = (settings.cockpit_worker_token or "").strip()
    if not expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    presented = (x_cockpit_worker_token or "").strip()
    if not presented or not compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    user = await _get_or_create_local_user(session)
    return AuthContext(actor_type="user", user=user)
