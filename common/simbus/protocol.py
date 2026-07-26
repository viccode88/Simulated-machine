"""plant-bus 協定：以換行分隔的 JSON 訊息（newline-delimited JSON over TCP）。

sim_net 只交換物理量與時間，不提供控制介面；外部 PLC 一律走 Modbus。
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_BUS_PORT = 7000


class MsgType(str, Enum):
    # 設備 -> 匯流排
    HELLO = "HELLO"
    TICK_DONE = "TICK_DONE"
    EVENT = "EVENT"
    SNAPSHOT_DATA = "SNAPSHOT_DATA"
    RESTORE_ACK = "RESTORE_ACK"
    FAULT_ACK = "FAULT_ACK"
    PONG = "PONG"
    # 匯流排 -> 設備
    WELCOME = "WELCOME"
    TICK = "TICK"
    SNAPSHOT_SAVE = "SNAPSHOT_SAVE"
    SNAPSHOT_RESTORE = "SNAPSHOT_RESTORE"
    FAULT = "FAULT"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    PING = "PING"


class Role(str, Enum):
    DEVICE = "device"        # 參與 lockstep，會回 TICK_DONE
    OBSERVER = "observer"    # 只收資料（historian、HMI）
    CONTROLLER = "controller"  # 只收事件與時間（DCS 走 Modbus，不用 sim_net 改物理量）


class SignalQuality(str, Enum):
    GOOD = "GOOD"
    STALE = "STALE"
    BAD = "BAD"
    UNCERTAIN = "UNCERTAIN"


def encode(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any]:
    return json.loads(line.decode("utf-8"))


class SignalValue:
    """帶品質的程序量。"""

    __slots__ = ("value", "quality", "tick", "source")

    def __init__(self, value: float, quality: str = SignalQuality.GOOD.value,
                 tick: int = 0, source: str = "") -> None:
        self.value = value
        self.quality = quality
        self.tick = tick
        self.source = source

    @property
    def good(self) -> bool:
        return self.quality == SignalQuality.GOOD.value

    @property
    def usable(self) -> bool:
        """STALE 仍可用（保持上一筆），BAD 不可信。"""
        return self.quality in (SignalQuality.GOOD.value, SignalQuality.STALE.value,
                                SignalQuality.UNCERTAIN.value)

    def to_dict(self) -> dict:
        return {"value": self.value, "quality": self.quality, "tick": self.tick, "source": self.source}

    @staticmethod
    def from_dict(data: dict) -> "SignalValue":
        return SignalValue(
            value=float(data.get("value", 0.0)),
            quality=str(data.get("quality", SignalQuality.BAD.value)),
            tick=int(data.get("tick", 0)),
            source=str(data.get("source", "")),
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"SignalValue({self.value!r}, {self.quality})"
