"""Store minuscule : 3 tables (usage_counters, leads, generation_costs).

Postgres en prod (DATABASE_URL=postgresql://…), SQLite en dev/tests (défaut).
Aucun contenu scrapé ni profil généré n'est stocké — uniquement des compteurs, des emails et le coût
de chaque génération (ni adresse, ni URL, ni texte).
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
        generations INTEGER NOT NULL DEFAULT 0,
        source      TEXT
    )""",
    # Une ligne par génération qui a appelé le modèle, réussie ou non : les échecs coûtent aussi.
    """CREATE TABLE IF NOT EXISTS generation_costs (
        created_at        TEXT NOT NULL,
        status            TEXT NOT NULL,
        pages             INTEGER NOT NULL DEFAULT 0,
        words             INTEGER NOT NULL DEFAULT 0,
        model             TEXT,
        llm_calls         INTEGER NOT NULL DEFAULT 0,
        prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost_llm_usd      REAL NOT NULL DEFAULT 0,
        cost_data_usd     REAL NOT NULL DEFAULT 0,
        cost_other_usd    REAL NOT NULL DEFAULT 0,
        cost_complete     INTEGER NOT NULL DEFAULT 0,
        cost_llm_source   TEXT
    )""",
]

COST_COLUMNS = ["created_at", "status", "pages", "words", "model", "llm_calls", "prompt_tokens",
                "completion_tokens", "cost_llm_usd", "cost_data_usd", "cost_other_usd", "cost_complete",
                "cost_llm_source"]
# Migrations additives (colonnes ajoutées après coup) — idempotentes, l'erreur "existe déjà" est ignorée
MIGRATIONS = [
    "ALTER TABLE leads ADD COLUMN source TEXT",
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
        for stmt in MIGRATIONS:
            try:
                with self._conn() as c:
                    c.execute(stmt)
            except Exception:  # colonne déjà présente
                pass

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
    def upsert_lead(self, email: str, consent: bool, source: str | None = None) -> None:
        email = email.strip().lower()
        ts = now_iso()
        sql = self._q(
            "INSERT INTO leads (email, created_at, consent_at, last_seen, source) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET last_seen = EXCLUDED.last_seen, "
            "consent_at = COALESCE(leads.consent_at, EXCLUDED.consent_at), "
            "source = COALESCE(EXCLUDED.source, leads.source)"
        )
        with self._conn() as c:
            c.execute(sql, (email, ts, ts if consent else None, ts, source))

    def mark_verified(self, email: str, source: str | None = None) -> None:
        ts = now_iso()
        sql = self._q("UPDATE leads SET verified_at = COALESCE(verified_at, %s), last_seen = %s, "
                      "source = COALESCE(%s, source) WHERE email = %s")
        with self._conn() as c:
            c.execute(sql, (ts, ts, source, email.strip().lower()))

    def bump_lead_generations(self, email: str) -> None:
        sql = self._q("UPDATE leads SET generations = generations + 1, last_seen = %s WHERE email = %s")
        with self._conn() as c:
            c.execute(sql, (now_iso(), email.strip().lower()))

    def get_lead(self, email: str) -> dict | None:
        sql = self._q("SELECT email, created_at, consent_at, verified_at, last_seen, generations, source FROM leads WHERE email = %s")
        with self._conn() as c:
            row = c.execute(sql, (email.strip().lower(),)).fetchone()
        if not row:
            return None
        keys = ["email", "created_at", "consent_at", "verified_at", "last_seen", "generations", "source"]
        return dict(zip(keys, row))

    # --- coût par génération ----------------------------------------------
    def record_generation_cost(self, *, status: str, pages: int, words: int, model: str | None,
                               meter) -> None:
        contract = meter.contract()
        values = (now_iso(), status, pages, words, model, meter.llm_calls, meter.prompt_tokens,
                  meter.completion_tokens, contract["cost_llm_usd"], contract["cost_data_usd"],
                  contract["cost_other_usd"], int(contract["cost_complete"]), meter.llm_source)
        sql = self._q(f"INSERT INTO generation_costs ({', '.join(COST_COLUMNS)}) "
                      f"VALUES ({', '.join(['%s'] * len(COST_COLUMNS))})")
        with self._conn() as c:
            c.execute(sql, values)

    def recent_generation_costs(self, limit: int = 100) -> list[dict]:
        sql = self._q(f"SELECT {', '.join(COST_COLUMNS)} FROM generation_costs ORDER BY created_at DESC LIMIT %s")
        with self._conn() as c:
            rows = c.execute(sql, (limit,)).fetchall()
        return [dict(zip(COST_COLUMNS, row)) for row in rows]


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
