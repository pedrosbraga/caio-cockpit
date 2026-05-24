"""Cloudflare Access JWT verification with manual JWKS key cache.

Design notes (post Codex security review):
- We DO NOT use PyJWKClient's auto-refresh-on-kid-miss feature: an attacker who
  can hit the origin can spam tokens with random kids and trigger blocking JWKS
  fetches on every request, draining the threadpool.
- Instead, we keep our own ``dict[kid, PyJWK]`` cache. Lookups are pure-Python,
  no I/O. Refresh is gated by a cooldown (``jwks_refresh_cooldown_s``, 60s
  default) and only fires on unknown kid OR TTL expiry.
- Refresh is atomic: new dict is built off-side via ``anyio.to_thread.run_sync``
  and only swapped under lock after fetch+parse succeed. Transient JWKS outages
  keep the old cache.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import anyio
import jwt
from jwt import PyJWK, PyJWKClient, PyJWKClientError
from pydantic import BaseModel

from app.core.logging import get_logger

logger = get_logger(__name__)


class CFAccessClaims(BaseModel):
    """Subset of CF Access JWT claims we trust after verification."""

    sub: str
    email: str
    aud: str | list[str]
    iss: str
    exp: int
    iat: int


class CFAccessError(Exception):
    """Raised when CF Access JWT verification fails. ``reason`` is machine-readable."""

    def __init__(self, reason: str, *, status_code: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass
class CFAccessVerifier:
    team_domain: str
    audience: str
    allowed_emails: frozenset[str]
    jwks_cache_ttl_s: int = 3600
    jwks_refresh_cooldown_s: int = 60
    jwks_fetch_timeout_s: float = 3.0

    _keys: dict[str, PyJWK] = field(default_factory=dict, init=False, repr=False)
    _keys_built_at: float = field(default=0.0, init=False, repr=False)
    _last_refresh_attempt_at: float = field(default=0.0, init=False, repr=False)
    _lock: asyncio.Lock = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}.cloudflareaccess.com"

    @property
    def jwks_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"

    def _fetch_jwks_sync(self) -> dict[str, PyJWK]:
        """Sync JWKS fetch. Returns new {kid: PyJWK} dict. Called via thread pool."""
        client = PyJWKClient(
            self.jwks_url,
            cache_keys=False,
            timeout=self.jwks_fetch_timeout_s,
        )
        jwks_data = client.fetch_data()
        new_keys: dict[str, PyJWK] = {}
        for key_data in jwks_data.get("keys", []):
            kid = key_data.get("kid")
            if not kid:
                continue
            try:
                new_keys[kid] = PyJWK(key_data)
            except Exception as exc:  # noqa: BLE001 — any parse error skips that one key
                logger.warning("auth.cf_access.jwks.parse_failed kid=%s err=%s", kid, exc)
        if not new_keys:
            raise PyJWKClientError("JWKS returned no usable keys")
        return new_keys

    async def _refresh_if_allowed(self, *, reason: str) -> dict[str, PyJWK] | None:
        """Attempt JWKS refresh respecting cooldown.

        Returns:
            New keys dict on success, ``None`` on cooldown or fetch failure.

        On fetch failure, ``self._keys`` is left untouched (defends against
        transient upstream outages).
        """
        now = time.monotonic()
        async with self._lock:
            if (now - self._last_refresh_attempt_at) < self.jwks_refresh_cooldown_s:
                logger.info("auth.cf_access.jwks.refresh_skipped_cooldown reason=%s", reason)
                return None
            self._last_refresh_attempt_at = now
            try:
                new_keys = await anyio.to_thread.run_sync(self._fetch_jwks_sync)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auth.cf_access.jwks.refresh_failed reason=%s keep_old=%d err=%s",
                    reason,
                    len(self._keys),
                    exc,
                )
                return None
            self._keys = new_keys
            self._keys_built_at = time.monotonic()
            logger.info("auth.cf_access.jwks.refresh ok reason=%s n_keys=%d", reason, len(new_keys))
            return new_keys

    async def _get_keys(self) -> dict[str, PyJWK]:
        """Return current keys. Refresh if empty (cold) or TTL elapsed.

        TTL expiry does NOT preemptively invalidate the cache. If refresh fails,
        we keep serving from the old cache.
        """
        now = time.monotonic()
        if not self._keys:
            await self._refresh_if_allowed(reason="cold_start")
            if not self._keys:
                raise CFAccessError("jwks_unavailable")
            return self._keys
        if (now - self._keys_built_at) >= self.jwks_cache_ttl_s:
            await self._refresh_if_allowed(reason="ttl_expired")
        return self._keys

    async def verify(self, token: str) -> CFAccessClaims:
        """Verify and return claims. Raises CFAccessError on any failure."""
        if not token:
            raise CFAccessError("missing_token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise CFAccessError("invalid_token_header") from exc
        kid = header.get("kid")
        if not kid:
            raise CFAccessError("missing_kid")

        keys = await self._get_keys()
        jwk = keys.get(kid)
        if jwk is None:
            new_keys = await self._refresh_if_allowed(reason="kid_miss")
            if new_keys is None or new_keys.get(kid) is None:
                raise CFAccessError("unknown_kid")
            jwk = new_keys[kid]

        signing_key = jwk.key

        try:
            payload = self._decode(token, signing_key)
        except jwt.InvalidSignatureError:
            new_keys = await self._refresh_if_allowed(reason="invalid_signature")
            if new_keys is None or new_keys.get(kid) is None:
                raise CFAccessError("invalid_signature")
            try:
                payload = self._decode(token, new_keys[kid].key)
            except jwt.InvalidSignatureError as exc:
                raise CFAccessError("invalid_signature") from exc
            except jwt.InvalidTokenError as exc:
                raise CFAccessError("invalid_token") from exc

        return self._build_claims(payload)

    def _decode(self, token: str, signing_key) -> dict:
        try:
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "aud", "iss", "sub", "email"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise CFAccessError("expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise CFAccessError("invalid_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise CFAccessError("invalid_issuer") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise CFAccessError(f"missing_claim:{exc.claim}") from exc
        except jwt.InvalidSignatureError:
            raise
        except jwt.InvalidTokenError as exc:
            raise CFAccessError("invalid_token") from exc

    def _build_claims(self, payload: dict) -> CFAccessClaims:
        try:
            claims = CFAccessClaims(
                sub=str(payload["sub"]),
                email=str(payload["email"]).strip().lower(),
                aud=payload["aud"],
                iss=str(payload["iss"]),
                exp=int(payload["exp"]),
                iat=int(payload["iat"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CFAccessError("invalid_claims") from exc

        if claims.email not in self.allowed_emails:
            logger.warning("auth.cf_access.email_denied email=%s", claims.email)
            raise CFAccessError("email_not_allowed", status_code=403)

        return claims
