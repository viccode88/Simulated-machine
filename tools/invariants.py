"""物理與安全不變量檢查（供情境測試與外部測試工具使用）。

重點不只是「不要崩潰」，更重要的是「即使協定層被亂打，
物理安全邏輯仍必須成立」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

INVENTORY_SIGNALS = [
    "boiler.water_mass_kg",
    "feedwater_tank.water_mass_kg",
    "condenser.hotwell_mass_kg",
]


@dataclass
class InvariantChecker:
    max_speed_rpm: float = 4500.0
    max_pressure_bar: float = 200.0
    inventory_tolerance_kg: float = 6000.0
    violations: list[str] = field(default_factory=list)
    _first_inventory: float | None = None
    _latched: dict[str, bool] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    samples: int = 0

    def update(self, state: dict) -> list[str]:
        if not state or "signals" not in state:
            return []
        new: list[str] = []
        signals = {name: float(data.get("value", 0.0)) for name, data in state["signals"].items()}
        sim_time = state.get("sim_time", 0.0)
        self.samples += 1

        # 1. 數值必須有限
        for name, value in signals.items():
            if math.isnan(value) or math.isinf(value):
                new.append(f"{sim_time}s {name} 非有限數值 ({value})")

        # 2. 物理上限
        speed = signals.get("turbine.speed_rpm")
        if speed is not None and speed > self.max_speed_rpm:
            new.append(f"{sim_time}s 轉速超出物理上限 {speed:.0f} RPM")
        pressure = signals.get("boiler.pressure_bar_abs")
        if pressure is not None and pressure > self.max_pressure_bar:
            new.append(f"{sim_time}s 鍋爐壓力超出上限 {pressure:.1f} bar")
        for name in ("boiler.level_pct", "feedwater_tank.level_pct", "condenser.hotwell_level_pct"):
            value = signals.get(name)
            if value is not None and (value < -20.0 or value > 140.0):
                new.append(f"{sim_time}s {name} 超出合理範圍 ({value:.1f}%)")

        # 3. 水量守恆（允許排污、溢流、補水造成的緩慢變化）
        inventory = sum(signals.get(name, 0.0) for name in INVENTORY_SIGNALS)
        if inventory > 0:
            if self._first_inventory is None:
                self._first_inventory = inventory
            elif abs(inventory - self._first_inventory) > self.inventory_tolerance_kg:
                new.append(
                    f"{sim_time}s 總水量偏離基準 {inventory - self._first_inventory:+.0f} kg"
                )

        # 4. 跳機鎖存不得自行消失（只能經由 reset）
        for device, info in (state.get("participants") or {}).items():
            tripped = bool(info.get("tripped"))
            if self._latched.get(device) and not tripped:
                # 允許：快照還原或操作員重置；這裡以快照世代變化判斷
                if state.get("snapshot_generation", 0) == self._last.get("generation", 0):
                    new.append(f"{sim_time}s {device} 跳機鎖存自行解除")
            self._latched[device] = tripped
        self._last["generation"] = state.get("snapshot_generation", 0)

        self.violations.extend(new)
        return new

    def reset_baseline(self) -> None:
        self._first_inventory = None
        self._latched.clear()

    def summary(self) -> dict:
        return {"samples": self.samples, "violations": len(self.violations),
                "details": self.violations[:20]}
