# ruff: noqa: INP001
"""Tests for the worker-or-user auth dependency mounted on /decisions routes."""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import auth as auth_module
from app.core.auth import AuthContext
from app.core.auth_mode import AuthMode
from app.core.config import settings
from app.core.worker_auth import get_user_or_worker_auth_context
from app.db.session import get_session

WORKER_TOKEN = "test-worker-token-" + ("x" * 50)


async def _make_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _build_app(session_maker: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/api/v1")

    @router.post("/worker-only")
    async def _worker_only(
        auth: AuthContext = Depends(get_user_or_worker_auth_context),
    ) -> dict:
        return {"user_id": auth.user.clerk_user_id if auth.user else None}

    app.include_router(router)

    async def _override_get_session():
        async with session_maker() as session:
            yield session
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[auth_module.get_session] = _override_get_session
    return app


@pytest.mark.asyncio
async def test_valid_worker_token_local_mode_returns_200(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", "different-bearer-not-the-worker-token-zzzzzzzzzzzz")
    monkeypatch.setattr(settings, "cockpit_worker_token", WORKER_TOKEN)
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/worker-only",
                headers={"X-Cockpit-Worker-Token": WORKER_TOKEN},
            )
        assert resp.status_code == 200, resp.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_worker_token_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", "valid-local-token-" + ("z" * 50))
    monkeypatch.setattr(settings, "cockpit_worker_token", WORKER_TOKEN)
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/worker-only",
                headers={"X-Cockpit-Worker-Token": "wrong-token"},
            )
        assert resp.status_code == 401
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_token_unset_in_settings_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", "valid-local-token-" + ("z" * 50))
    monkeypatch.setattr(settings, "cockpit_worker_token", "")  # unset
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/worker-only",
                headers={"X-Cockpit-Worker-Token": "anything"},
            )
        assert resp.status_code == 401
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_bearer_local_mode_takes_precedence_over_worker_token(monkeypatch):
    """If a valid user auth resolves (e.g. LOCAL bearer), worker token is ignored."""
    local_token = "valid-local-token-" + ("z" * 50)
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", local_token)
    monkeypatch.setattr(settings, "cockpit_worker_token", WORKER_TOKEN)
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/worker-only",
                headers={
                    "Authorization": f"Bearer {local_token}",
                    "X-Cockpit-Worker-Token": "wrong-token-doesnt-matter",
                },
            )
        assert resp.status_code == 200
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_auth_at_all_returns_401(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", AuthMode.LOCAL)
    monkeypatch.setattr(settings, "local_auth_token", "valid-local-token-" + ("z" * 50))
    monkeypatch.setattr(settings, "cockpit_worker_token", WORKER_TOKEN)
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_app(session_maker)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/v1/worker-only")
        assert resp.status_code == 401
    finally:
        await engine.dispose()
