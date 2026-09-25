"""巨灾事件通知台账的SQLite保存，与赔案记录表分离。"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """事件台账存取：cat_events保存登记与预估，cat_event_entries保存报案/补证历史。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    estimated_total_loss REAL NOT NULL,
                    reported_by TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cat_event_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_pk INTEGER NOT NULL REFERENCES cat_events(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    estimated_total_loss REAL NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cat_event_entries ON cat_event_entries(event_pk, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, event_id: str, occurred_at: str, estimated_total_loss: float, reported_by: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO cat_events(event_id,occurred_at,estimated_total_loss,reported_by,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (event_id, occurred_at, float(estimated_total_loss), reported_by, 1, now, now),
                )
                event_pk = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO cat_event_entries(event_pk,kind,actor_id,estimated_total_loss,note,created_at) VALUES(?,?,?,?,?,?)",
                    (event_pk, "report", actor_id, float(estimated_total_loss), "首次报案登记", now),
                )
                row = connection.execute("SELECT * FROM cat_events WHERE id=?", (event_pk,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件已登记，后续赔案请追加到已有事件") from exc
        return self._row(row)

    def find_by_event_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
        return self._row(row) if row is not None else None

    def get(self, event_pk: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE id=?", (event_pk,)).fetchone()
        if row is None:
            raise NotFound("事件不存在")
        return self._row(row)

    def list_events(self, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM cat_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, event_pk: int, expected_version: int, estimated_total_loss: float, actor_id: str, kind: str, note: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM cat_events WHERE id=?", (event_pk,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("事件不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE cat_events SET estimated_total_loss=?,version=?,updated_at=? WHERE id=?",
                (float(estimated_total_loss), version, now, event_pk),
            )
            connection.execute(
                "INSERT INTO cat_event_entries(event_pk,kind,actor_id,estimated_total_loss,note,created_at) VALUES(?,?,?,?,?,?)",
                (event_pk, kind, actor_id, float(estimated_total_loss), note or "", now),
            )
            result = connection.execute("SELECT * FROM cat_events WHERE id=?", (event_pk,)).fetchone()
            connection.commit()
        return self._row(result)

    def entries(self, event_pk: int) -> List[Dict[str, Any]]:
        self.get(event_pk)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM cat_event_entries WHERE event_pk=? ORDER BY id", (event_pk,)).fetchall()
        return [dict(row) for row in rows]
