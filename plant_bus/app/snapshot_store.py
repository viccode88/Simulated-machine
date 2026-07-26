"""快照儲存庫：可重現的 plant snapshot。

檔案格式（JSON）：
{
  "meta": {"name","created","sim_time","tick","devices","description","tags","checksum"},
  "bus":  {"tick","sim_time","signals"},
  "participants": {"boiler": {...}, "dcs-plc": {...}}
}
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

SNAPSHOT_FORMAT = 1


class SnapshotStore:
    def __init__(self, directory: str) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in "-_.")
        if not safe:
            raise ValueError("快照名稱不合法")
        return os.path.join(self.directory, f"{safe}.json")

    @staticmethod
    def _checksum(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def save(self, name: str, bus_state: dict, participants: dict[str, Any],
             description: str = "", tags: list[str] | None = None) -> dict:
        payload = {"format": SNAPSHOT_FORMAT, "bus": bus_state, "participants": participants}
        meta = {
            "name": name,
            "created": time.time(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "sim_time": bus_state.get("sim_time", 0.0),
            "tick": bus_state.get("tick", 0),
            "devices": sorted(participants.keys()),
            "description": description,
            "tags": tags or [],
            "checksum": self._checksum(payload),
        }
        document = {"meta": meta, **payload}
        path = self._path(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)  # 原子寫入
        return meta

    def load(self, name: str) -> dict:
        with open(self._path(name), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def exists(self, name: str) -> bool:
        return os.path.exists(self._path(name))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def list(self) -> list[dict]:
        items: list[dict] = []
        for filename in os.listdir(self.directory):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.directory, filename), "r", encoding="utf-8") as handle:
                    document = json.load(handle)
                meta = document.get("meta", {})
                meta.setdefault("name", filename[:-5])
                items.append(meta)
            except Exception as exc:  # pragma: no cover
                items.append({"name": filename[:-5], "error": repr(exc)})
        items.sort(key=lambda m: m.get("created", 0), reverse=True)
        return items

    def verify(self, name: str) -> bool:
        document = self.load(name)
        meta = document.get("meta", {})
        payload = {k: v for k, v in document.items() if k != "meta"}
        return self._checksum(payload) == meta.get("checksum")
