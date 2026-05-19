"""Smoke tests for CritiquesSqliteReader (F2.3, V1.1 Reflexion bridge).

We exercise the reader directly with a temp SQLite fixture so we do not depend
on the FastAPI app, the bind mount, or Caio's real ``critiques.sqlite``. The
three cases below cover the only behaviors the rest of the stack relies on:

- ``enabled=False`` short-circuits to ``status="disabled"`` (no I/O).
- A missing DB file becomes ``status="error"`` and is *never* raised.
- A real (temp) DB returns rows mapped 1:1 to the schema, ordered DESC.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.services.caio_bridge import CritiquesSqliteReader


def _make_fixture(tmp_path: Path) -> Path:
    db = tmp_path / "critiques.sqlite"
    conn = sqlite3.connect(db.as_posix())
    try:
        conn.executescript(
            """
            CREATE TABLE critiques (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at       TEXT NOT NULL,
                approval_log_id    INTEGER NOT NULL UNIQUE,
                jid                TEXT,
                action             TEXT NOT NULL,
                contact_message    TEXT,
                caio_suggestion    TEXT,
                final_response     TEXT,
                miss               TEXT,
                hit                TEXT,
                pattern            TEXT,
                confidence         REAL,
                raw_llm_response   TEXT
            );
            """
        )
        conn.executemany(
            "INSERT INTO critiques (generated_at, approval_log_id, jid, action, "
            "contact_message, caio_suggestion, final_response, miss, hit, "
            "pattern, confidence, raw_llm_response) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "2026-05-17T21:00:00+00:00",
                    101,
                    "553898518080@s.whatsapp.net",
                    "replaced",
                    "msg do contato",
                    "sugestao caio",
                    "resposta final do pedro",
                    "miss texto",
                    "hit texto",
                    "pattern aprendido",
                    0.91,
                    "{huge raw llm payload not surfaced}",
                ),
                (
                    "2026-05-10T21:00:00+00:00",
                    102,
                    None,
                    "rejected",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "outro pattern mais antigo",
                    0.65,
                    None,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_reader_disabled_returns_disabled_status(tmp_path: Path) -> None:
    db = _make_fixture(tmp_path)
    reader = CritiquesSqliteReader(db_path=db, enabled=False)
    result = asyncio.run(reader.recent_critiques(limit=10))
    assert result.status == "disabled"
    assert result.data is None


def test_reader_missing_file_returns_error_without_raise(tmp_path: Path) -> None:
    reader = CritiquesSqliteReader(
        db_path=tmp_path / "nope.sqlite",
        enabled=True,
        timeout_s=1.0,
    )
    result = asyncio.run(reader.recent_critiques(limit=10))
    assert result.status == "error"
    # OperationalError is what the reader raises on missing file; BridgeBase
    # captures the class name into error_class.
    assert result.error_class == "OperationalError"
    assert result.data is None


def test_reader_real_sqlite_fixture(tmp_path: Path) -> None:
    db = _make_fixture(tmp_path)
    reader = CritiquesSqliteReader(db_path=db, enabled=True, timeout_s=2.0)

    result = asyncio.run(reader.recent_critiques(limit=10))

    assert result.status == "ok", result
    assert isinstance(result.data, list)
    assert len(result.data) == 2

    newest, older = result.data
    # Newest first (ORDER BY generated_at DESC).
    assert newest["generated_at"] == "2026-05-17T21:00:00+00:00"
    assert newest["approval_log_id"] == 101
    assert newest["action"] == "replaced"
    assert newest["pattern"] == "pattern aprendido"
    assert newest["confidence"] == pytest.approx(0.91)
    # raw_llm_response is intentionally not in the returned dict.
    assert "raw_llm_response" not in newest

    assert older["approval_log_id"] == 102
    assert older["jid"] is None
    assert older["miss"] is None


def test_reader_since_filter_excludes_older_rows(tmp_path: Path) -> None:
    db = _make_fixture(tmp_path)
    reader = CritiquesSqliteReader(db_path=db, enabled=True, timeout_s=2.0)

    # Cutoff between the two fixture rows (newest is 2026-05-17, older 2026-05-10).
    result = asyncio.run(
        reader.recent_critiques(
            limit=10, since_iso="2026-05-15T00:00:00+00:00"
        )
    )

    assert result.status == "ok"
    assert isinstance(result.data, list)
    assert len(result.data) == 1
    assert result.data[0]["approval_log_id"] == 101
