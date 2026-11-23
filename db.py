"""Camada de persistência com suporte a SQLite (dev) e Postgres (produção).

A escolha é feita pela presença de DATABASE_URL no ambiente:
- sem DATABASE_URL  -> SQLite local (DB_PATH)
- com DATABASE_URL  -> Postgres (psycopg3)

Responsabilidades:
- criar o schema (usuários e pagamentos);
- controlar tokens com operações atômicas;
- garantir idempotência de cobranças e créditos (nunca creditar duas vezes).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import Settings

try:
    import psycopg
except ImportError:  # psycopg é opcional em desenvolvimento local
    psycopg = None  # type: ignore[assignment]


class _CursorWrapper:
    """Adaptador para os métodos usados pelo código (execute/fetchone/fetchall/rowcount)."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, sql: str, params: tuple = ()) -> "_CursorWrapper":
        self._cur.execute(sql, params)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cur.fetchall())

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount)


class _ConnectionWrapper:
    """Une a interface sqlite3 e psycopg sob os métodos que usamos."""

    def __init__(self, conn: Any, placeholder: str, postgres: bool) -> None:
        self._conn = conn
        self.placeholder = placeholder
        self.postgres = postgres

    def _convert(self, sql: str) -> str:
        return sql.replace("?", self.placeholder) if self.postgres else sql

    def execute(self, sql: str, params: tuple = ()) -> _CursorWrapper:
        return _CursorWrapper(self._conn.execute(self._convert(sql), params))

    def executemany(self, sql: str, seq: list[tuple]) -> _CursorWrapper:
        return _CursorWrapper(self._conn.executemany(self._convert(sql), seq))

    def executescript(self, script: str) -> None:
        if self.postgres:
            with self._conn.cursor() as cur:
                cur.execute(script)
        else:
            self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


class Database:
    """Wrapper thread-safe para SQLite ou Postgres."""

    def __init__(self, path: Path, settings: Settings) -> None:
        self._settings = settings
        self._path = path
        self._database_url = os.getenv("DATABASE_URL", "").strip()
        self._postgres = bool(self._database_url)
        self._placeholder = "%s" if self._postgres else "?"
        self._local = threading.local()
        self._write_lock = threading.Lock()

        if self._postgres and psycopg is None:
            raise RuntimeError(
                "DATABASE_URL definida mas psycopg não está instalado. "
                "Rode: pip install 'psycopg[binary]'"
            )

        self._init_schema()

    # ------------------------------------------------------------------ conexão
    def _connection(self) -> _ConnectionWrapper:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._postgres:
                from psycopg.rows import dict_row

                raw = psycopg.connect(
                    self._database_url, autocommit=False, row_factory=dict_row
                )
            else:
                raw = sqlite3.connect(
                    self._path, timeout=30, isolation_level=None
                )
                raw.row_factory = sqlite3.Row
                raw.execute("PRAGMA journal_mode=WAL;")
                raw.execute("PRAGMA busy_timeout=30000;")
                raw.execute("PRAGMA foreign_keys=ON;")
            conn = _ConnectionWrapper(raw, self._placeholder, self._postgres)
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[_ConnectionWrapper]:
        """Transação de escrita serializada por processo."""
        with self._write_lock:
            conn = self._connection()
            if self._postgres:
                # Nova transação a cada bloco (psycopg inicia implicitamente).
                pass
            else:
                conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _init_schema(self) -> None:
        serial = "SERIAL" if self._postgres else "INTEGER"
        check = "CHECK (credited IN (0, 1))" if not self._postgres else "CHECK (credited IN (0, 1))"
        script = f"""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                tokens       INTEGER NOT NULL DEFAULT 0,
                last_request REAL NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id            {serial} PRIMARY KEY,
                user_id       INTEGER NOT NULL REFERENCES users(user_id),
                provider      TEXT NOT NULL,
                charge_id     TEXT NOT NULL,
                amount_cents  INTEGER NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                credited      INTEGER NOT NULL DEFAULT 0 {check},
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                UNIQUE(provider, charge_id)
            );

            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
        """
        conn = self._connection()
        conn.executescript(script)
        conn.commit()

    # --------------------------------------------------------------------- users
    def get_or_create_user(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> Any:
        """Garante que o usuário exista; novo usuário ganha tokens grátis."""
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO users
                        (user_id, username, first_name, tokens, last_request, created_at)
                    VALUES (?, ?, ?, ?, 0, ?)
                    """,
                    (
                        user_id,
                        username,
                        first_name,
                        self._settings.free_tokens,
                        now,
                    ),
                )
            else:
                # Mantém metadados atualizados sem mexer no saldo.
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username, first_name, user_id),
                )
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row

    def get_user(self, user_id: int) -> Any:
        return self._connection().execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()

    def get_tokens(self, user_id: int) -> int:
        row = self.get_user(user_id)
        return int(row["tokens"]) if row else 0

    def cooldown_remaining(self, user_id: int) -> float:
        """Retorna segundos restantes de cooldown (0 se já pode pedir)."""
        row = self.get_user(user_id)
        if row is None:
            return 0.0
        elapsed = time.time() - float(row["last_request"])
        return max(0.0, self._settings.cooldown_seconds - elapsed)

    def touch_request(self, user_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE users SET last_request = ? WHERE user_id = ?",
                (time.time(), user_id),
            )

    def consume_token(self, user_id: int) -> bool:
        """Debita 1 token somente se houver saldo. Retorna True se debitou."""
        with self.transaction() as conn:
            cur = conn.execute(
                """
                UPDATE users
                   SET tokens = tokens - 1
                 WHERE user_id = ? AND tokens >= 1
                """,
                (user_id,),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ payments
    def register_charge(
        self,
        user_id: int,
        provider: str,
        charge_id: str,
        amount_cents: int,
    ) -> None:
        """Registra cobrança criada. UNIQUE garante idempotência por charge_id."""
        now = time.time()
        conflict = (
            "ON CONFLICT (provider, charge_id) DO NOTHING"
            if self._postgres
            else "OR IGNORE"
        )
        with self.transaction() as conn:
            conn.execute(
                f"""
                INSERT {conflict} INTO payments
                    (user_id, provider, charge_id, amount_cents, status,
                     credited, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (user_id, provider, charge_id, amount_cents, now, now),
            )

    def mark_approved_and_credit(self, provider: str, charge_id: str) -> bool:
        """Aprova pagamento e credita tokens exatamente uma vez.

        Retorna True somente na primeira transição para credited=1.
        """
        with self.transaction() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, credited
                  FROM payments
                 WHERE provider = ? AND charge_id = ?
                """,
                (provider, charge_id),
            ).fetchone()
            if row is None:
                return False
            if row["credited"]:
                return False

            now = time.time()
            cur = conn.execute(
                """
                UPDATE payments
                   SET status = 'approved', credited = 1, updated_at = ?
                 WHERE id = ? AND credited = 0
                """,
                (now, row["id"]),
            )
            if cur.rowcount != 1:
                return False

            conn.execute(
                "UPDATE users SET tokens = tokens + ? WHERE user_id = ?",
                (self._settings.tokens_per_payment, row["user_id"]),
            )
            return True

    def mark_status(self, provider: str, charge_id: str, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE payments
                   SET status = ?, updated_at = ?
                 WHERE provider = ? AND charge_id = ?
                """,
                (status, time.time(), provider, charge_id),
            )

    def pending_charges(self, provider: str) -> list[Any]:
        return list(
            self._connection().execute(
                """
                SELECT * FROM payments
                 WHERE provider = ? AND credited = 0 AND status != 'approved'
                """,
                (provider,),
            ).fetchall()
        )
