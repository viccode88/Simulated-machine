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


class SnapshotIntegrityError(Exception):
    """快照損毀、格式不符或內容不完整，不得用於還原。"""


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
             description: str = "", tags: list[str] | None = None,
             missing: list[str] | None = None) -> dict:
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
            # 離線設備造成的 partial snapshot 必須留下記號，還原時才能拒絕，
            # 避免出現「部分設備新狀態、部分設備舊狀態」的混合機組
            "missing": sorted(missing or []),
            "complete": not missing,
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

    def load(self, name: str, verify: bool = False) -> dict:
        """讀取快照。verify=True 時會檢查格式與 checksum，不通過則拋出例外。"""
        try:
            with open(self._path(name), "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SnapshotIntegrityError(f"快照 {name} 不是合法 JSON：{exc}") from exc
        if verify:
            self.check(name, document)
        return document

    def check(self, name: str, document: dict | None = None) -> dict:
        """驗證快照可用於還原；回傳 document，不通過則拋出 SnapshotIntegrityError。"""
        if document is None:
            document = self.load(name)
        if not isinstance(document, dict):
            raise SnapshotIntegrityError(f"快照 {name} 結構不正確")
        meta = document.get("meta")
        if not isinstance(meta, dict):
            raise SnapshotIntegrityError(f"快照 {name} 缺少 meta")
        if document.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotIntegrityError(
                f"快照 {name} 格式版本不符（{document.get('format')} != {SNAPSHOT_FORMAT}）"
            )
        for key in ("bus", "participants"):
            if not isinstance(document.get(key), dict):
                raise SnapshotIntegrityError(f"快照 {name} 缺少 {key} 區段")
        expected = meta.get("checksum")
        if not expected:
            raise SnapshotIntegrityError(f"快照 {name} 沒有 checksum")
        payload = {k: v for k, v in document.items() if k != "meta"}
        actual = self._checksum(payload)
        if actual != expected:
            raise SnapshotIntegrityError(
                f"快照 {name} checksum 不符（檔案 {expected}，實際 {actual}），內容可能已損毀"
            )
        return document

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
        try:
            self.check(name)
        except (SnapshotIntegrityError, OSError):
            return False
        return True
