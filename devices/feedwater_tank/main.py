"""給水槽（除氧器）設備容器。

dM/dt = Mcondensate_in - Mfeedwater_out - Moverflow
Level = 100 × (M - Mmin) / (Mmax - Mmin)
"""
from __future__ import annotations

import math

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.modbus.encoding import enc_i16, enc_u16, enc_u32
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp, first_order

CODE = 5600


def saturation_pressure_bar(temp_c: float) -> float:
    """Antoine 反算飽和壓力（bar），用於除氧器內壓。"""
    temp = clamp(temp_c, 1.0, 220.0)
    mmhg = 10.0 ** (8.07131 - 1730.63 / (233.426 + temp))
    return max(0.01, mmhg / 750.062)


class FeedwaterTank(BaseDevice):
    NAME = "feedwater_tank"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "HOLD_LAST"
    HAS_START_STOP = False

    PROCESS_INPUTS = [
        RegSpec(9, "TANK_LEVEL", "%", 100),
        RegSpec(10, "TANK_MASS_HI", "kg", 1, dtype="u32"),
        RegSpec(11, "TANK_MASS_LO", "kg", 1),
        RegSpec(12, "CONDENSATE_INFLOW", "kg/s", 100),
        RegSpec(13, "FEEDWATER_OUTFLOW", "kg/s", 100),
        RegSpec(14, "WATER_TEMPERATURE", "degC", 10, dtype="i16"),
        RegSpec(15, "TANK_PRESSURE", "bar(a)", 1000),
        RegSpec(16, "OVERFLOW_FLOW", "kg/s", 100),
        RegSpec(17, "NET_FLOW", "kg/s", 100, dtype="i16"),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True, desc="給水槽水位設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="除氧加熱輸出"),
        RegSpec(29, "HEATING_SETPOINT", "degC", 10, lo=20, hi=250, writable=True, desc="加熱溫度設定"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "LOW_LEVEL", 0, "給水槽水位低"),
        AlarmSpec(CODE + 12, "LOW_LOW_LEVEL", 1, "給水槽水位低低"),
        AlarmSpec(CODE + 13, "HIGH_LEVEL", 2, "給水槽水位高"),
        AlarmSpec(CODE + 14, "OVERFLOW", 3, "給水槽溢流"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "LOW_LEVEL", "signal": "level", "direction": "low",
         "alarm_code": CODE + 11, "message": "Feedwater tank level low"},
        {"code": CODE + 2, "name": "HIGH_LEVEL", "signal": "level", "direction": "high",
         "alarm_code": CODE + 13, "message": "Feedwater tank level high"},
    ]

    PUBLISHES = ["feedwater_tank.level_pct", "feedwater_tank.pressure_bar_abs",
                 "feedwater_tank.water_mass_kg", "feedwater_tank.temp_c"]
    SUBSCRIBES = ["condensate_pump.flow_kg_s", "feedwater_pump.flow_kg_s",
                  "condenser.condensate_temp_c"]

    STATE_VARS = ["water_mass", "level", "temperature", "pressure", "inflow", "outflow",
                  "overflow", "net_flow"]

    def configure(self) -> None:
        c = self.cfg.get("feedwater_tank", {})
        self.mass_min = float(c.get("mass_min_kg", 5000.0))
        self.mass_max = float(c.get("mass_max_kg", 35000.0))
        self.heating_tau = float(c.get("heating_tau_s", 60.0))
        self.overflow_gain = float(c.get("overflow_gain_kg_s_per_pct", 5.0))
        self.low_low_level = float(c.get("low_low_level_pct", 15.0))

        self.water_mass = float(c.get("initial_mass_kg",
                                      self.mass_min + 0.6 * (self.mass_max - self.mass_min)))
        self.level = self._level(self.water_mass)
        self.temperature = float(c.get("initial_temp_c", 40.0))
        self.pressure = saturation_pressure_bar(self.temperature)
        self.inflow = 0.0
        self.outflow = 0.0
        self.overflow = 0.0
        self.net_flow = 0.0
        # 給水槽是被動容器，永遠處於 RUNNING
        self.sm.force(DeviceState.RUNNING, "PASSIVE_VESSEL")

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "feedwater_tank.level_setpoint_pct", 60.0)),
            "MANUAL_OUTPUT": 100.0,
            "HEATING_SETPOINT": float(cfg_get(self.cfg, "feedwater_tank.heating_setpoint_c", 150.0)),
        }

    def _level(self, mass: float) -> float:
        return 100.0 * (mass - self.mass_min) / (self.mass_max - self.mass_min)

    def control_output(self) -> float:
        return self.hr("MANUAL_OUTPUT")

    def start_permissives(self) -> list[tuple[str, bool]]:
        return [("被動容器", True)]

    def step(self, dt: float) -> None:
        self.inflow = max(0.0, self.sig("condensate_pump.flow_kg_s", 0.0))
        self.outflow = max(0.0, self.sig("feedwater_pump.flow_kg_s", 0.0))
        leak = self.faults.factor("tank_leak_kg_s", 0.0)

        self.water_mass += (self.inflow - self.outflow - leak) * dt
        self.water_mass = max(0.0, self.water_mass)

        # 溢流
        if self.water_mass > self.mass_max:
            excess = self.water_mass - self.mass_max
            self.overflow = min(excess / max(dt, 1e-6), self.overflow_gain * 20.0)
            self.water_mass -= self.overflow * dt
        else:
            self.overflow = 0.0

        self.level = self._level(self.water_mass)
        self.net_flow = self.inflow - self.outflow

        # 溫度：加熱器輸出 + 進水溫度混合
        heating = self.hr("MANUAL_OUTPUT") / 100.0
        inlet_temp = self.sig("condenser.condensate_temp_c", 40.0)
        target = inlet_temp + (self.hr("HEATING_SETPOINT") - inlet_temp) * heating
        self.temperature = first_order(self.temperature, target, self.heating_tau, dt)
        self.pressure = saturation_pressure_bar(self.temperature)

        self.alarms.set(CODE + 14, self.overflow > 0.01, self.overflow, 0.0)
        self.alarms.set(CODE + 12, self.level < self.low_low_level, self.level, self.low_low_level)
        self.mass_total += self.inflow * dt

    def apply_comm_loss(self, policy: str) -> None:
        # 給水槽無主動輸出，僅標記通訊品質
        return

    def protection_values(self) -> dict[str, float]:
        level, _ = self.faults.sensor("level", self.level, self.dt)
        return {"level": level, "inflow": self.inflow, "outflow": self.outflow}

    def publish(self) -> dict[str, float]:
        level, _ = self.faults.sensor("level", self.level, self.dt)
        return {
            "feedwater_tank.level_pct": level,
            "feedwater_tank.pressure_bar_abs": self.pressure,
            "feedwater_tank.water_mass_kg": self.water_mass,
            "feedwater_tank.temp_c": self.temperature,
        }

    def fill_registers(self, regs: list[int]) -> None:
        level, _ = self.faults.sensor("level", self.level, self.dt)
        regs[9] = enc_u16(max(0.0, level), 100)
        regs[10], regs[11] = enc_u32(self.water_mass)
        regs[12] = enc_u16(self.inflow, 100)
        regs[13] = enc_u16(self.outflow, 100)
        regs[14] = enc_i16(self.temperature, 10)
        regs[15] = enc_u16(self.pressure, 1000)
        regs[16] = enc_u16(self.overflow, 100)
        regs[17] = enc_i16(clamp(self.net_flow, -300, 300), 100)


if __name__ == "__main__":
    run_device(FeedwaterTank)
