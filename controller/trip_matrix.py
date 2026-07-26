"""跳機矩陣：設備跳機 -> 全廠連鎖動作。

設備本身已透過 sim_net 訊號互鎖（例如汽輪機跳機 -> 主蒸汽閥快關），
這裡是 DCS 層額外的動作，確保即使某條互鎖失效仍有第二層防護。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TripAction:
    device: str
    action: str          # write_holding / pulse_coil
    target: str          # 暫存器名稱
    value: float = 0.0
    reason: str = ""


@dataclass
class TripRule:
    source: str          # 來源設備
    description: str
    actions: list[TripAction] = field(default_factory=list)


DEFAULT_MATRIX: list[TripRule] = [
    TripRule(
        source="turbine",
        description="汽輪機跳機 -> 斷路器立即打開、主蒸汽閥快速關閉",
        actions=[
            TripAction("generator", "pulse_coil", "BREAKER_OPEN", reason="TURBINE_TRIP"),
            TripAction("steam_valve", "write_holding", "MANUAL_OUTPUT", 0.0, "TURBINE_TRIP"),
            TripAction("generator", "write_holding", "PRIMARY_SETPOINT", 0.0, "TURBINE_TRIP"),
        ],
    ),
    TripRule(
        source="boiler",
        description="鍋爐跳機 -> 燃燒器歸零、主蒸汽閥關閉",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "BOILER_TRIP"),
            TripAction("steam_valve", "write_holding", "MANUAL_OUTPUT", 0.0, "BOILER_TRIP"),
        ],
    ),
    TripRule(
        source="condenser",
        description="冷凝器高壓 -> 降載",
        actions=[
            TripAction("generator", "write_holding", "PRIMARY_SETPOINT", 0.0, "CONDENSER_TRIP"),
        ],
    ),
    TripRule(
        source="feedwater_pump",
        description="給水泵跳機 -> 降低燃燒率，避免鍋爐低水位",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "FEEDWATER_PUMP_TRIP"),
        ],
    ),
    TripRule(
        source="condensate_pump",
        description="凝結水泵跳機 -> 給水泵降速，保護給水槽",
        actions=[
            TripAction("feedwater_pump", "write_holding", "MANUAL_OUTPUT", 20.0,
                       "CONDENSATE_PUMP_TRIP"),
        ],
    ),
    TripRule(
        source="steam_valve",
        description="主蒸汽閥故障 -> 停止燃燒",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "STEAM_VALVE_FAULT"),
        ],
    ),
]


class TripMatrix:
    def __init__(self, rules: list[TripRule] | None = None,
                 emit: Callable[..., None] | None = None) -> None:
        self.rules = rules or DEFAULT_MATRIX
        self._emit = emit
        self._fired: set[str] = set()

    def evaluate(self, tripped: dict[str, bool]) -> list[TripAction]:
        """回傳需要執行的動作；同一來源只在跳機邊緣觸發一次。"""
        actions: list[TripAction] = []
        for rule in self.rules:
            active = bool(tripped.get(rule.source, False))
            if active and rule.source not in self._fired:
                self._fired.add(rule.source)
                actions.extend(rule.actions)
                if self._emit:
                    self._emit("TRIP_MATRIX_FIRED", source=rule.source,
                               description=rule.description,
                               actions=[a.target for a in rule.actions])
            elif not active and rule.source in self._fired:
                self._fired.discard(rule.source)
                if self._emit:
                    self._emit("TRIP_MATRIX_CLEARED", source=rule.source)
        return actions

    def to_dict(self) -> dict:
        return {"fired": sorted(self._fired)}

    def from_dict(self, data: dict) -> None:
        self._fired = set(data.get("fired") or [])
