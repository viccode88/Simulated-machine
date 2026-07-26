"""狀態持久化（SQLite）。

容器重新啟動後：
* 不得自動清除跳機。
* 設備預設回到安全輸出。
* 需要操作員執行 reset / start。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_state (
    key         TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS boot_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wall_time   REAL NOT NULL,
    note        TEXT
);
"""


class StateStore:
    def __init__(self, path: str, device: str) -> None:
        self.path = path
        self.device = device
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._dirty = False
        self._last_save = 0.0

    # -- 讀寫 --------------------------------------------------------------
    def load(self, key: str = "state") -> dict[str, Any]:
        row = self.conn.execute("SELECT payload FROM device_state WHERE key=?", (key,)).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    def save(self, data: dict[str, Any], key: str = "state") -> None:
        self.conn.execute(
            "INSERT INTO device_state(key,payload,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, json.dumps(data, ensure_ascii=False, default=str), time.time()),
        )
        self._last_save = time.monotonic()
        self._dirty = False

    def maybe_save(self, data_fn, period: float = 1.0, key: str = "state") -> bool:
        """節流儲存：預設每秒一次。"""
        now = time.monotonic()
        if now - self._last_save < period:
            return False
        self.save(data_fn(), key=key)
        return True

    def log_boot(self, note: str = "") -> int:
        cur = self.conn.execute("INSERT INTO boot_log(wall_time,note) VALUES(?,?)", (time.time(), note))
        count = self.conn.execute("SELECT COUNT(*) FROM boot_log").fetchone()[0]
        return int(count)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


class NullStore(StateStore):  # pragma: no cover - 測試用
    def __init__(self, device: str = "test") -> None:
        self.device = device
        self.path = ":memory:"
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.executescript(SCHEMA)
        self._dirty = False
        self._last_save = 0.0
