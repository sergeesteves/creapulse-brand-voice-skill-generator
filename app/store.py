"""Store minuscule pour le gating : 2 tables (usage_counters, leads).

Postgres en prod (DATABASE_URL=postgresql://…), SQLite en dev/tests (défaut).
Aucun contenu scrapé ni profil généré n'est stocké — uniquement des compteurs et des emails.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS usage_counters (
        day   TEXT NOT NULL,
        scope TEXT NOT NULL,
        key   TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, scope, key)
    )""",
    """CREATE TABLE IF NOT EXISTS leads (
        email       TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL,
        consent_at  TEXT,
        verified_at TEXT,
        last_seen   TEXT,
        generations INTEGER NOT NULL DEFAULT 0
    )""",
]


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    """Interface commune ; deux backends (sqlite, postgres)."""

    def __init__(self, url: str | None = None):
        self.url = url or settings.database_url
        self.is_pg = self.url.startswith(("postgres://", "postgresql://"))
        self._lock = threading.Lock()
        self._pool = None
        self._sqlite_path = None
        if self.is_pg:
            from psycopg_pool import ConnectionPool  # import tardif : optionnel en dev
            self._pool = ConnectionPool(self.url, min_size=0, max_size=4, open=True, kwargs={"autocommit": True})
        else:
            self._sqlite_path = self.url.removeprefix("sqlite:///")
        self.init_schema()

    # --- connexions -------------------------------------------------------
    @contextmanager
    def _conn(self) -> Iterator:
        if self.is_pg:
            with self._pool.connection() as conn:
                yield conn
        else:
            with self._lock:
                conn = sqlite3.connect(self._sqlite_path, isolation_level=None)
                try:
                    yield conn
                finally:
                    conn.close()

    def _q(self, sql: str) -> str:
        return sql if self.is_pg else sql.replace("%s", "?")

    def init_schema(self) -> None:
        with self._conn() as c:
            for stmt in SCHEMA:
                c.execute(stmt)

    def close(self) -> None:
        if self._pool:
            self._pool.close()

    # --- compteurs --------------------------------------------------------
    def increment(self, scope: str, key: str, day: str | None = None) -> int:
        """Incrémente atomiquement et renvoie la nouvelle valeur."""
        day = day or today()
        sql = self._q(
            "INSERT INTO usage_counters (day, scope, key, count) VALUES (%s, %s, %s, 1) "
            "ON CONFLICT (day, scope, key) DO UPDATE SET count = usage_counters.count + 1 "
            "RETURNING count"
        )
        with self._conn() as c:
            cur = c.execute(sql, (day, scope, key))
            row = cur.fetchone()
            return int(row[0])

    def decrement(self, scope: str, key: str, day: str | None = None) -> None:
        day = day or today()
        sql = self._q("UPDATE usage_counters SET count = CASE WHEN count > 0 THEN count - 1 ELSE 0 END "
                      "WHERE day = %s AND scope = %s AND key = %s")
        with self._conn() as c:
            c.execute(sql, (day, scope, key))

    def get(self, scope: str, key: str, day: str | None = None) -> int:
        day = day or today()
        sql = self._q("SELECT count FROM usage_counters WHERE day = %s AND scope = %s AND key = %s")
        with self._conn() as c:
            row = c.execute(sql, (day, scope, key)).fetchone()
            return int(row[0]) if row else 0

    def purge_old_counters(self, keep_days: int = 7) -> None:
        limit = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).strftime("%Y-%m-%d")
        with self._conn() as c:
            c.execute(self._q("DELETE FROM usage_counters WHERE day < %s"), (limit,))

    # --- leads ------------------------------------------------------------
    def upsert_lead(self, email: str, consent: bool) -> None:
        email = email.strip().lower()
        ts = now_iso()
        sql = self._q(
            "INSERT INTO leads (email, created_at, consent_at, last_seen) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET last_seen = EXCLUDED.last_seen, "
            "consent_at = COALESCE(leads.consent_at, EXCLUDED.consent_at)"
        )
        with self._conn() as c:
            c.execute(sql, (email, ts, ts if consent else None, ts))

    def mark_verified(self, email: str) -> None:
        ts = now_iso()
        sql = self._q("UPDATE leads SET verified_at = COALESCE(verified_at, %s), last_seen = %s WHERE email = %s")
        with self._conn() as c:
            c.execute(sql, (ts, ts, email.strip().lower()))

    def bump_lead_generations(self, email: str) -> None:
        sql = self._q("UPDATE leads SET generations = generations + 1, last_seen = %s WHERE email = %s")
        with self._conn() as c:
            c.execute(sql, (now_iso(), email.strip().lower()))

    def get_lead(self, email: str) -> dict | None:
        sql = self._q("SELECT email, created_at, consent_at, verified_at, last_seen, generations FROM leads WHERE email = %s")
        with self._conn() as c:
            row = c.execute(sql, (email.strip().lower(),)).fetchone()
        if not row:
            return None
        keys = ["email", "created_at", "consent_at", "verified_at", "last_seen", "generations"]
        return dict(zip(keys, row))


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
