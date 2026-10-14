"""SQLite 存储：称重记录与计划设置。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

# 自动上报（如 ESP32）一次称重可能分几包到达：体重、阻抗先后上报，这个窗口内合并为一条
MERGE_WINDOW = timedelta(minutes=2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS weighins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    impedance_low REAL,
    impedance_high REAL,
    heart_rate INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weighins_measured_at ON weighins(measured_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_OPTIONAL_FIELDS = ("impedance_low", "impedance_high", "heart_rate")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_profile(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'profile'").fetchone()
        return json.loads(row["value"]) if row else None

    def save_profile(self, profile: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('profile', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(profile, ensure_ascii=False),),
            )

    def list_weighins(self, limit: int | None = None, newest_first: bool = False) -> list[dict]:
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM weighins ORDER BY measured_at {order}, id {order}"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def add_weighin(
        self,
        measured_at: datetime,
        weight_kg: float,
        impedance_low: float | None = None,
        impedance_high: float | None = None,
        heart_rate: int | None = None,
        source: str = "manual",
        merge: bool = False,
    ) -> dict:
        """新增一条称重；merge=True 时与同来源、时间相近的上一条合并。"""
        values = {
            "impedance_low": impedance_low,
            "impedance_high": impedance_high,
            "heart_rate": heart_rate,
        }
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._conn() as conn:
            if merge:
                prev = conn.execute(
                    "SELECT * FROM weighins WHERE source = ? ORDER BY measured_at DESC, id DESC LIMIT 1",
                    (source,),
                ).fetchone()
                if prev and abs(measured_at - datetime.fromisoformat(prev["measured_at"])) <= MERGE_WINDOW:
                    merged = {k: values[k] if values[k] is not None else prev[k] for k in _OPTIONAL_FIELDS}
                    conn.execute(
                        "UPDATE weighins SET weight_kg = ?, impedance_low = ?, impedance_high = ?, "
                        "heart_rate = ? WHERE id = ?",
                        (weight_kg, *(merged[k] for k in _OPTIONAL_FIELDS), prev["id"]),
                    )
                    row = conn.execute("SELECT * FROM weighins WHERE id = ?", (prev["id"],)).fetchone()
                    return dict(row)
            cur = conn.execute(
                "INSERT INTO weighins (measured_at, weight_kg, impedance_low, impedance_high, "
                "heart_rate, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    measured_at.isoformat(timespec="seconds"),
                    weight_kg,
                    *(values[k] for k in _OPTIONAL_FIELDS),
                    source,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM weighins WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def delete_weighin(self, weighin_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM weighins WHERE id = ?", (weighin_id,))
            return cur.rowcount > 0
