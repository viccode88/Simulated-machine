"""故障注入（僅 LAB_MODE=true 時開放）。

協定層故障在 common/modbus/server.py（CommFaults），
這裡只處理感測器、執行器與程序故障，兩者分開才能判斷問題來源。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from ..modbus.register_map import Quality

SENSOR_MODES = (
    "none", "stuck_at", "bias", "noise", "drift", "intermittent",
    "out_of_range", "freeze", "bad_quality",
)


@dataclass
class SensorFault:
    mode: str = "none"
    value: float = 0.0        # stuck_at / out_of_range 的固定值
    bias: float = 0.0         # 正/負偏差
    noise: float = 0.0        # 標準差
    drift: float = 0.0        # 每秒漂移量
    probability: float = 0.1  # intermittent 失效機率
    quality: int | None = None
    _drift_acc: float = 0.0
    _frozen: float | None = None

    def apply(self, true_value: float, dt: float) -> tuple[float, int]:
        quality = int(Quality.GOOD) if self.quality is None else int(self.quality)
        if self.mode == "none":
            return true_value, quality
        if self.mode == "stuck_at":
            return self.value, int(Quality.SIMULATED_FAULT)
        if self.mode == "bias":
            return true_value + self.bias, int(Quality.UNCERTAIN)
        if self.mode == "noise":
            return true_value + random.gauss(0.0, self.noise), int(Quality.UNCERTAIN)
        if self.mode == "drift":
            self._drift_acc += self.drift * dt
            return true_value + self._drift_acc, int(Quality.UNCERTAIN)
        if self.mode == "intermittent":
            if random.random() < self.probability:
                return self.value, int(Quality.BAD_SENSOR)
            return true_value, quality
        if self.mode == "out_of_range":
            return self.value, int(Quality.OUT_OF_RANGE)
        if self.mode == "freeze":
            if self._frozen is None:
                self._frozen = true_value
            return self._frozen, int(Quality.STALE)
        if self.mode == "bad_quality":
            # 數值保留但品質 BAD
            return true_value, int(Quality.BAD_SENSOR)
        return true_value, quality

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "value": self.value, "bias": self.bias, "noise": self.noise,
            "drift": self.drift, "probability": self.probability, "quality": self.quality,
            "_drift_acc": self._drift_acc, "_frozen": self._frozen,
        }

    @staticmethod
    def from_dict(data: dict) -> "SensorFault":
        fault = SensorFault()
        for key, value in data.items():
            if hasattr(fault, key):
                setattr(fault, key, value)
        return fault


@dataclass
class FaultInjector:
    enabled: bool = False
    sensors: dict[str, SensorFault] = field(default_factory=dict)
    actuators: dict[str, Any] = field(default_factory=dict)
    process: dict[str, float] = field(default_factory=dict)

    # -- 套用 --------------------------------------------------------------
    def sensor(self, name: str, true_value: float, dt: float) -> tuple[float, int]:
        if not self.enabled:
            return true_value, int(Quality.GOOD)
        fault = self.sensors.get(name)
        if fault is None:
            return true_value, int(Quality.GOOD)
        return fault.apply(true_value, dt)

    def actuator(self, name: str, default: Any = None) -> Any:
        if not self.enabled:
            return default
        return self.actuators.get(name, default)

    def factor(self, name: str, default: float = 1.0) -> float:
        """程序故障倍率/量值，例如 cooling_water_availability、boiler_leak_kg_s。"""
        if not self.enabled:
            return default
        return float(self.process.get(name, default))

    def has(self, name: str) -> bool:
        return self.enabled and (
            name in self.actuators or name in self.process or name in self.sensors
        )

    # -- 設定 --------------------------------------------------------------
    def set(self, category: str, name: str, spec: Any) -> None:
        if category == "sensor":
            if isinstance(spec, dict):
                self.sensors[name] = SensorFault.from_dict(spec)
            else:
                self.sensors.pop(name, None)
        elif category == "actuator":
            if spec is None:
                self.actuators.pop(name, None)
            else:
                self.actuators[name] = spec
        elif category == "process":
            if spec is None:
                self.process.pop(name, None)
            else:
                self.process[name] = float(spec)
        else:
            raise ValueError(f"未知故障類別: {category}")

    def clear(self, category: str | None = None, name: str | None = None) -> None:
        if category is None:
            self.sensors.clear()
            self.actuators.clear()
            self.process.clear()
            return
        targets = {"sensor": self.sensors, "actuator": self.actuators, "process": self.process}
        if category not in targets:
            # 與 set() 一致拋 ValueError；未知類別（例如 comm）不得變成 KeyError
            raise ValueError(f"未知故障類別: {category}")
        target = targets[category]
        if name is None:
            target.clear()
        else:
            target.pop(name, None)

    def active_word(self) -> int:
        bits = [
            bool(self.sensors),
            any(f.mode in ("stuck_at", "freeze") for f in self.sensors.values()),
            bool(self.actuators),
            bool(self.process),
        ]
        return sum(1 << i for i, b in enumerate(bits) if b)

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "sensors": {k: v.mode for k, v in self.sensors.items()},
            "actuators": dict(self.actuators),
            "process": dict(self.process),
        }

    # -- 快照 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "sensors": {k: v.to_dict() for k, v in self.sensors.items()},
            "actuators": dict(self.actuators),
            "process": dict(self.process),
        }

    def from_dict(self, data: dict) -> None:
        self.enabled = bool(data.get("enabled", self.enabled))
        self.sensors = {k: SensorFault.from_dict(v) for k, v in (data.get("sensors") or {}).items()}
        self.actuators = dict(data.get("actuators") or {})
        self.process = {k: float(v) for k, v in (data.get("process") or {}).items()}
