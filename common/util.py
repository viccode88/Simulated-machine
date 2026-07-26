"""共通工具：設定載入、限幅、速率限制、時間、事件記錄。"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

# --------------------------------------------------------------------------
# 數值工具
# --------------------------------------------------------------------------


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def rate_limit(current: float, target: float, up_rate: float, down_rate: float, dt: float) -> float:
    """以 up_rate / down_rate（單位/秒）限制 current 往 target 移動。"""
    delta = target - current
    if delta > 0:
        return current + min(delta, up_rate * dt)
    return current + max(delta, -down_rate * dt)


def first_order(current: float, target: float, tau: float, dt: float) -> float:
    """一階遲滯 dX/dt = (target - X) / tau。"""
    if tau <= 0:
        return target
    return current + (target - current) * (dt / tau) if dt < tau else target


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * clamp(t, 0.0, 1.0)


def ramp(x: float, x0: float, x1: float) -> float:
    """x<=x0 -> 0, x>=x1 -> 1，中間線性。x0 可大於 x1（反向）。"""
    if x1 == x0:
        return 1.0 if x >= x1 else 0.0
    return clamp((x - x0) / (x1 - x0), 0.0, 1.0)


# --------------------------------------------------------------------------
# 時間
# --------------------------------------------------------------------------


def wall_time_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def monotonic() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(*paths: str) -> dict[str, Any]:
    """依序載入並深層合併 YAML 設定檔（後者覆蓋前者）。找不到的檔案會被忽略。"""
    merged: dict[str, Any] = {}
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError(f"設定檔 {path} 必須是 mapping")
        merged = _deep_merge(merged, data)
    return merged


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def cfg_get(cfg: dict, path: str, default: Any = None) -> Any:
    """以 "a.b.c" 取得巢狀設定值。"""
    node: Any = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# --------------------------------------------------------------------------
# 事件記錄（JSON Lines）
# --------------------------------------------------------------------------


@dataclass
class EventLogger:
    """輸出 JSON Lines 至 stdout 與（可選）檔案，同時保留環形緩衝。"""

    device: str
    path: str | None = None
    ring_size: int = 500
    ring: list[dict] = field(default_factory=list)
    _sink: Any = None
    sim_time_fn: Any = None

    def __post_init__(self) -> None:
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._sink = open(self.path, "a", encoding="utf-8")

    def emit(self, event: str, **fields: Any) -> dict:
        record = {
            "wall_time": wall_time_iso(),
            "sim_time": round(self.sim_time_fn(), 3) if self.sim_time_fn else None,
            "device": self.device,
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        if os.environ.get("EVENT_LOG_STDOUT", "1") != "0":
            print(line, flush=True)
        if self._sink:
            self._sink.write(line + "\n")
            self._sink.flush()
        self.ring.append(record)
        if len(self.ring) > self.ring_size:
            del self.ring[: len(self.ring) - self.ring_size]
        return record

    def close(self) -> None:
        if self._sink:
            self._sink.close()
            self._sink = None


def install_excepthook(logger: EventLogger) -> None:
    """未捕捉例外一律寫成事件，方便崩潰分析。"""
    import traceback

    def _hook(exc_type, exc_value, exc_tb):  # pragma: no cover - 只在崩潰時執行
        logger.emit(
            "PYTHON_EXCEPTION",
            code=9001,
            message=f"{exc_type.__name__}: {exc_value}",
            traceback="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
