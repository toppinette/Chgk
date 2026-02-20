from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class PersistedUserState:
    user_id: int
    rating_email: Optional[str]
    rating_token: Optional[str]
    role: Optional[str]


class BotStorage:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_state (
                    user_id INTEGER PRIMARY KEY,
                    rating_email TEXT,
                    rating_token TEXT,
                    role TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(user_state)").fetchall()
            }
            if "rating_email" not in columns:
                conn.execute("ALTER TABLE user_state ADD COLUMN rating_email TEXT")

    def get_user_state(self, user_id: int) -> PersistedUserState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, rating_email, rating_token, role FROM user_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if row is None:
            return PersistedUserState(
                user_id=user_id,
                rating_email=None,
                rating_token=None,
                role=None,
            )

        return PersistedUserState(
            user_id=row["user_id"],
            rating_email=row["rating_email"],
            rating_token=row["rating_token"],
            role=row["role"],
        )

    def upsert_role(self, user_id: int, role: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, role, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    role = excluded.role,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, role),
            )

    def upsert_rating_token(self, user_id: int, token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, rating_token, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    rating_token = excluded.rating_token,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, token),
            )

    def upsert_rating_email(self, user_id: int, email: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, rating_email, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    rating_email = excluded.rating_email,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, email),
            )

    def clear_rating_token(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, rating_token, updated_at)
                VALUES (?, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    rating_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id,),
            )
