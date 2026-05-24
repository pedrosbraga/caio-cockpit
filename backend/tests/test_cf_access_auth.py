# ruff: noqa: INP001
"""Unit + integration tests for Cloudflare Access JWT auth mode."""

from __future__ import annotations

import json
import time
from typing import Iterable
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK, PyJWKClientError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.api.users import router as users_router
from app.core import auth as auth_module
from app.core import cf_access_verifier as verifier_module
from app.core.auth_mode import AuthMode
from app.core.cf_access import CFAccessError, CFAccessVerifier
from app.core.config import settings
from app.db.session import get_session

TEAM = "test-team"
AUD = "test-aud-deadbeef"
EMAIL = "pedro@example.com"
ALLOWED = frozenset({EMAIL.lower()})
KID = "kid-1"


def _rsa_keypair() -> tuple[str, dict]:
    """Return (private_pem, jwk_dict) for a fresh RSA-2048 keypair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    numbers = key.public_key().public_numbers()

    def _b64uint(n: int) -> str:
        import base64
        b = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64uint(numbers.n),
        "e": _b64uint(numbers.e),
    }
    return pem_private.decode(), jwk


def _build_jwt(
    private_pem: str,
    *,
    email: str = EMAIL,
    aud: str = AUD,
    iss: str | None = None,
    exp_delta: int = 300,
    kid: str = KID,
    missing_claim: str | None = None,
) -> str:
    iss = iss if iss is not None else f"https://{TEAM}.cloudflareaccess.com"
    now = int(time.time())
    payload: dict = {
        "sub": "abc-123",
        "email": email,
        "aud": aud,
        "iss": iss,
        "exp": now + exp_delta,
        "iat": now,
    }
    if missing_claim:
        payload.pop(missing_claim, None)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def _patch_fetch(monkeypatch, jwk_dicts: Iterable[dict], counter: dict | None = None) -> None:
    """Patch CFAccessVerifier._fetch_jwks_sync to return PyJWK objects.

    If ``counter`` is given, increments ``counter['n']`` on each call so tests
    can assert fetch frequency.
    """
    keys = {jwk["kid"]: PyJWK(jwk) for jwk in jwk_dicts}

    def _fake(self):
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        if not keys:
            raise PyJWKClientError("no keys")
        return dict(keys)

    monkeypatch.setattr(CFAccessVerifier, "_fetch_jwks_sync", _fake)


def _make_verifier(**kw) -> CFAccessVerifier:
    defaults = dict(
        team_domain=TEAM,
        audience=AUD,
        allowed_emails=ALLOWED,
        jwks_cache_ttl_s=3600,
        jwks_refresh_cooldown_s=60,
        jwks_fetch_timeout_s=3.0,
    )
    defaults.update(kw)
    return CFAccessVerifier(**defaults)


# ---------------- Pure verifier unit tests ----------------


@pytest.mark.asyncio
async def test_valid_jwt_returns_claims(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    claims = await v.verify(_build_jwt(priv))
    assert claims.email == EMAIL.lower()
    assert claims.sub == "abc-123"
    assert claims.aud == AUD


@pytest.mark.asyncio
async def test_missing_token_raises(monkeypatch):
    _patch_fetch(monkeypatch, [])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify("")
    assert exc_info.value.reason == "missing_token"


@pytest.mark.asyncio
async def test_malformed_token_raises_invalid_token_header(monkeypatch):
    _patch_fetch(monkeypatch, [])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify("not.a.jwt")
    assert exc_info.value.reason == "invalid_token_header"


@pytest.mark.asyncio
async def test_missing_kid_in_header_raises(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    # Build a JWT without kid header
    now = int(time.time())
    payload = {
        "sub": "x", "email": EMAIL, "aud": AUD,
        "iss": f"https://{TEAM}.cloudflareaccess.com",
        "exp": now + 100, "iat": now,
    }
    token = jwt.encode(payload, priv, algorithm="RS256")  # no kid header
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(token)
    assert exc_info.value.reason == "missing_kid"


@pytest.mark.asyncio
async def test_expired_jwt_raises_expired(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv, exp_delta=-1))
    assert exc_info.value.reason == "expired"
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_audience_raises_invalid_audience(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv, aud="wrong-aud"))
    assert exc_info.value.reason == "invalid_audience"


@pytest.mark.asyncio
async def test_wrong_issuer_raises_invalid_issuer(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv, iss="https://other-team.cloudflareaccess.com"))
    assert exc_info.value.reason == "invalid_issuer"


@pytest.mark.asyncio
async def test_bad_signature_raises_invalid_signature(monkeypatch):
    # Sign with key A, expose key B's pubkey in JWKS — signature won't verify
    priv_a, _ = _rsa_keypair()
    _, jwk_b = _rsa_keypair()
    # Both jwks have SAME kid so the verifier finds a key but it's wrong
    jwk_b["kid"] = KID
    _patch_fetch(monkeypatch, [jwk_b])
    v = _make_verifier(jwks_refresh_cooldown_s=9999)  # block refresh-retry
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv_a))
    assert exc_info.value.reason == "invalid_signature"


@pytest.mark.asyncio
async def test_email_not_allowed_raises_403(monkeypatch):
    priv, jwk = _rsa_keypair()
    _patch_fetch(monkeypatch, [jwk])
    v = _make_verifier()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv, email="someoneelse@example.com"))
    assert exc_info.value.reason == "email_not_allowed"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_unknown_kid_with_cooldown_elapsed_then_refresh(monkeypatch):
    """Token with unknown kid → refresh → key still missing → unknown_kid."""
    priv, jwk = _rsa_keypair()
    counter = {"n": 0}
    _patch_fetch(monkeypatch, [jwk], counter=counter)
    v = _make_verifier(jwks_refresh_cooldown_s=0)  # always allow refresh
    # First request kid=KID (in cache after cold start)
    await v.verify(_build_jwt(priv))
    initial_fetches = counter["n"]
    # Now send a token signed with same key but kid="other" (not in JWKS)
    bad_token = _build_jwt(priv, kid="other")
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(bad_token)
    assert exc_info.value.reason == "unknown_kid"
    # Should have triggered at least one extra refresh
    assert counter["n"] > initial_fetches


@pytest.mark.asyncio
async def test_unknown_kid_in_cooldown_no_fetch(monkeypatch):
    """During cooldown, unknown kid must NOT trigger JWKS HTTP fetch."""
    priv, jwk = _rsa_keypair()
    counter = {"n": 0}
    _patch_fetch(monkeypatch, [jwk], counter=counter)
    # Large cooldown so the second call is blocked
    v = _make_verifier(jwks_refresh_cooldown_s=99999)
    await v.verify(_build_jwt(priv))  # cold start → 1 fetch
    fetches_after_cold = counter["n"]
    assert fetches_after_cold == 1
    bad_token = _build_jwt(priv, kid="other")
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(bad_token)
    assert exc_info.value.reason == "unknown_kid"
    # No additional fetch during cooldown
    assert counter["n"] == fetches_after_cold


@pytest.mark.asyncio
async def test_transient_jwks_failure_keeps_old_cache(monkeypatch):
    """If refresh fetch raises, old _keys is kept and valid tokens still work."""
    priv, jwk = _rsa_keypair()
    # First fetch succeeds, second raises
    call_state = {"calls": 0}

    def _fake_fetch(self):
        call_state["calls"] += 1
        if call_state["calls"] == 1:
            return {jwk["kid"]: PyJWK(jwk)}
        raise PyJWKClientError("upstream blip")

    monkeypatch.setattr(CFAccessVerifier, "_fetch_jwks_sync", _fake_fetch)
    v = _make_verifier(jwks_cache_ttl_s=0, jwks_refresh_cooldown_s=0)  # force TTL expire path
    # Cold start succeeds
    claims1 = await v.verify(_build_jwt(priv))
    assert claims1.email == EMAIL.lower()
    # Next verify will see TTL expired, attempt refresh → fails → keeps old keys
    claims2 = await v.verify(_build_jwt(priv))
    assert claims2.email == EMAIL.lower()


@pytest.mark.asyncio
async def test_cold_start_jwks_failure_raises_unavailable(monkeypatch):
    def _broken(self):
        raise PyJWKClientError("upstream down")
    monkeypatch.setattr(CFAccessVerifier, "_fetch_jwks_sync", _broken)
    v = _make_verifier(jwks_refresh_cooldown_s=0)
    priv, _ = _rsa_keypair()
    with pytest.raises(CFAccessError) as exc_info:
        await v.verify(_build_jwt(priv))
    assert exc_info.value.reason == "jwks_unavailable"


# ---------------- Dispatcher / ASGI integration tests ----------------


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _build_test_app(session_maker: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(users_router)
    app.include_router(api_v1)

    async def _override_get_session() -> AsyncSession:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[auth_module.get_session] = _override_get_session
    return app


def _install_verifier(monkeypatch, jwks: list[dict]) -> None:
    """Force a fresh singleton verifier for the test."""
    verifier = _make_verifier()
    _patch_fetch(monkeypatch, jwks)
    monkeypatch.setattr(verifier_module, "_verifier", verifier)


@pytest.mark.asyncio
async def test_dispatcher_cf_access_valid_token_returns_200(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.CF_ACCESS)
    priv, jwk = _rsa_keypair()
    _install_verifier(monkeypatch, [jwk])
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    token = _build_jwt(priv)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/api/v1/users/me",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["email"] == EMAIL.lower()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_cf_access_missing_header_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.CF_ACCESS)
    _install_verifier(monkeypatch, [])
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.get("/api/v1/users/me")
        assert resp.status_code == 401
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_cf_access_invalid_token_always_raises_even_optional(monkeypatch):
    """Optional auth path: header present but invalid must raise, not return None."""
    monkeypatch.setattr(settings, "auth_mode", AuthMode.CF_ACCESS)
    priv, jwk = _rsa_keypair()
    _install_verifier(monkeypatch, [jwk])
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    from fastapi import Depends, FastAPI as FA
    app = FA()
    api_v1 = APIRouter(prefix="/api/v1")

    @api_v1.get("/optional-check")
    async def _optional(ctx=Depends(auth_module.get_auth_context_optional)):
        return {"has_user": ctx is not None}

    app.include_router(api_v1)

    async def _override_get_session():
        async with session_maker() as session:
            yield session
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[auth_module.get_session] = _override_get_session

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            # Send EXPIRED token
            bad = _build_jwt(priv, exp_delta=-10)
            resp = await client.get(
                "/api/v1/optional-check",
                headers={"Cf-Access-Jwt-Assertion": bad},
            )
        assert resp.status_code == 401  # NOT 200 with has_user=False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dispatcher_duplicate_cf_access_header_returns_400(monkeypatch):
    """Two Cf-Access-Jwt-Assertion headers must be rejected as 400."""
    monkeypatch.setattr(settings, "auth_mode", AuthMode.CF_ACCESS)
    priv, jwk = _rsa_keypair()
    _install_verifier(monkeypatch, [jwk])
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)
    token = _build_jwt(priv)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            # httpx accepts list of (name, value) tuples to send duplicate headers
            resp = await client.get(
                "/api/v1/users/me",
                headers=[
                    ("cf-access-jwt-assertion", token),
                    ("CF-Access-Jwt-Assertion", token),  # mixed case
                ],
            )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body.get("detail") == "duplicate_cf_access_header"
    finally:
        await engine.dispose()
