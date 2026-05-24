"""Process-wide singleton for :class:`CFAccessVerifier`.

Kept in a separate module to avoid circular imports between ``cf_access`` and
``config``.
"""

from __future__ import annotations

import threading

from app.core.cf_access import CFAccessVerifier
from app.core.config import settings

_lock = threading.Lock()
_verifier: CFAccessVerifier | None = None


def get_cf_access_verifier() -> CFAccessVerifier:
    """Return the process-wide CFAccessVerifier, building it lazily."""
    global _verifier
    if _verifier is not None:
        return _verifier
    with _lock:
        if _verifier is not None:
            return _verifier
        _verifier = CFAccessVerifier(
            team_domain=settings.cf_access_team_domain,
            audience=settings.cf_access_audience,
            allowed_emails=settings.cf_access_allowed_emails_set,
            jwks_cache_ttl_s=settings.cf_access_jwks_cache_ttl_s,
            jwks_refresh_cooldown_s=settings.cf_access_jwks_refresh_cooldown_s,
            jwks_fetch_timeout_s=settings.cf_access_jwks_fetch_timeout_s,
        )
        return _verifier


def reset_cf_access_verifier() -> None:
    """Test helper: drop the singleton so the next call re-reads settings."""
    global _verifier
    with _lock:
        _verifier = None
