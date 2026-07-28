"""冷凝器設備容器（自持）。

本地自持控制：熱井有水就自行啟動冷卻水與真空系統，並以補水閥把熱井水位
維持在 40011 SECONDARY_SETPOINT。冷卻能力在自動模式固定全開（真空是所有
下游設備的允許條件，沒有理由自行降載）。

冷凝能力 Mcapacity = RatedCapacity × CoolingWaterAvailability × HeatTransferFactor
壓力     dP/dt = Koverload × ExcessSteam - Kvacuum × (P - Pminimum)
熱井     dMhotwell/dt = Mcondensed - Mcondensate_pump
"""
from __future__ import annotations

import math

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.modbus.encoding import enc_i16, enc_u16, enc_u32
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp, first_order, ramp

CODE = 5400


def saturation_temp_c(pressure_bar: float) -> float:
    """Antoine 方程式（1～100°C 範圍適用），用於低壓凝結水溫度。"""
    mmhg = max(1e-3, pressure_bar * 750.062)
    return clamp(1730.63 / (8.07131 - math.log10(mmhg)) - 233.426, 1.0, 220.0)


class Condenser(BaseDevice):
    NAME = "condenser"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "LOCAL_AUTO"

    PROCESS_INPUTS = [
        RegSpec(9, "CONDENSER_PRESSURE", "bar(a)", 10000),
        RegSpec(10, "VACUUM", "kPa", 100),
        RegSpec(11, "HOTWELL_LEVEL", "%", 100),
        RegSpec(12, "EXHAUST_INFLOW", "kg/s", 100),
        RegSpec(13, "CONDENSING_CAPACITY", "kg/s", 100),
        RegSpec(14, "ACTUAL_CONDENSED", "kg/s", 100),
        RegSpec(15, "CONDENSATE_OUTFLOW", "kg/s", 100),
        RegSpec(16, "COOLING_WATER_AVAILABILITY", "%", 100),
        RegSpec(17, "VACUUM_SYSTEM_OUTPUT", "%", 100),
        RegSpec(18, "CONDENSATE_TEMPERATURE", "degC", 10, dtype="i16"),
        RegSpec(19, "HOTWELL_MASS_HI", "kg", 1, dtype="u32"),
        RegSpec(20, "HOTWELL_MASS_LO", "kg", 1),
        RegSpec(21, "MAKEUP_VALVE_POSITION", "%", 100, desc="補水閥實際開度（本地控制）"),
        RegSpec(22, "MAKEUP_FLOW", "kg/s", 100),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "bar(a)", 10000, lo=0, hi=6.5, writable=True,
                desc="冷凝器壓力設定值"),
        RegSpec(10, "SECONDARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True,
                desc="熱井水位設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True,
                desc="手動冷卻能力命令"),
        RegSpec(29, "MAKEUP_VALVE_CMD", "%", 100, lo=0, hi=100, writable=True, desc="補水閥命令"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "HIGH_PRESSURE", 0, "冷凝器壓力高"),
        AlarmSpec(CODE + 12, "LOW_HOTWELL_LEVEL", 1, "熱井水位低"),
        AlarmSpec(CODE + 13, "HIGH_HOTWELL_LEVEL", 2, "熱井水位高"),
        AlarmSpec(CODE + 14, "COOLING_WATER_LOW", 3, "冷卻水能力不足"),
        AlarmSpec(CODE + 15, "VACUUM_SYSTEM_FAULT", 4, "真空系統異常"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "HIGH_PRESSURE", "signal": "pressure", "direction": "high",
         "alarm_code": CODE + 11, "message": "Condenser pressure high"},
        {"code": CODE + 2, "name": "LOW_HOTWELL_LEVEL", "signal": "hotwell_level",
         "direction": "low", "alarm_code": CODE + 12, "message": "Hotwell level low"},
        {"code": CODE + 3, "name": "HIGH_HOTWELL_LEVEL", "signal": "hotwell_level",
         "direction": "high", "alarm_code": CODE + 13, "message": "Hotwell level high"},
    ]

    PUBLISHES = ["condenser.pressure_bar_abs", "condenser.hotwell_level_pct",
                 "condenser.condensed_kg_s", "condenser.capacity_kg_s",
                 "condenser.condensate_temp_c", "condenser.hotwell_mass_kg"]
    SUBSCRIBES = ["turbine.exhaust_flow_kg_s", "condensate_pump.flow_kg_s"]

    STATE_VARS = ["pressure", "hotwell_mass", "hotwell_level", "capacity", "condensed",
                  "exhaust_in", "condensate_out", "cooling_availability", "vacuum_output",
                  "condensate_temp", "makeup_flow", "makeup_command"]

    def configure(self) -> None:
        c = self.cfg.get("condenser", {})
        self.rated_capacity = float(c.get("rated_capacity_kg_s", 110.0))
        self.k_overload = float(c.get("k_overload", 0.02))
        self.k_vacuum = float(c.get("k_vacuum", 0.05))
        self.p_min_base = float(c.get("min_pressure_bar", 0.04))
        self.p_min_load = float(c.get("min_pressure_per_flow", 0.0004))
        self.mass_min = float(c.get("mass_min_kg", 5000.0))
        self.mass_max = float(c.get("mass_max_kg", 25000.0))
        self.heat_transfer_factor = float(c.get("heat_transfer_factor", 1.0))
        self.vacuum_pull_time = float(c.get("vacuum_pull_time_s", 60.0))
        self.max_makeup = float(c.get("max_makeup_kg_s", 20.0))
        self.air_leak_gain = float(c.get("air_leak_gain", 0.05))

        self.pressure = float(c.get("initial_pressure_bar", 1.0))
        self.hotwell_mass = float(c.get("initial_mass_kg",
                                        self.mass_min + 0.5 * (self.mass_max - self.mass_min)))
        self.hotwell_level = self._level(self.hotwell_mass)
        self.capacity = 0.0
        self.condensed = 0.0
        self.exhaust_in = 0.0
        self.condensate_out = 0.0
        self.cooling_availability = 0.0
        self.vacuum_output = 0.0
        self.condensate_temp = 25.0
        self.makeup_flow = 0.0
        # 補水閥命令是本地控制量，不回寫自己的 Holding Register
        # （40030 屬於操作端；設備回寫會與 PLC 的週期寫入互相蓋來蓋去）
        self.makeup_command = float(cfg_get(self.cfg, "condenser.makeup_default_pct", 0.0))
        self.makeup_gain = float(c.get("makeup_gain_pct_per_pct_s", 5.0))

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "condenser.pressure_setpoint_bar", 0.08)),
            "SECONDARY_SETPOINT": float(cfg_get(self.cfg, "condenser.hotwell_setpoint_pct", 55.0)),
            "MANUAL_OUTPUT": 100.0,
            "MAKEUP_VALVE_CMD": 0.0,
        }

    def _level(self, mass: float) -> float:
        return 100.0 * (mass - self.mass_min) / (self.mass_max - self.mass_min)

    def control_output(self) -> float:
        return self.cooling_availability * 100.0

    def start_permissives(self) -> list[tuple[str, bool]]:
        return [
            ("熱井水位可用", self.hotwell_level > 5.0),
            ("無跳機鎖存", not self.protection.any_latched),
            ("緊急停止未啟動", not self.estop),
        ]

    def on_start(self) -> None:
        self.sm.to(DeviceState.STARTING, "START")

    def on_stop(self) -> None:
        self.sm.to(DeviceState.STOPPING, "STOP")

    # -- 物理 --------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.exhaust_in = max(0.0, self.sig("turbine.exhaust_flow_kg_s", 0.0))
        self.condensate_out = max(0.0, self.sig("condensate_pump.flow_kg_s", 0.0))

        running = self.sm.state in (DeviceState.STARTING, DeviceState.RUNNING)
        # 自動模式：冷卻能力全開（真空是汽輪機的允許條件，自持設備不會自行降載）
        if not running:
            self.local_output = 0.0
            command = 0.0
        elif self.auto_mode:
            self.local_output = 100.0
            command = 1.0
        else:
            self.local_output = clamp(self.hr("MANUAL_OUTPUT"), 0.0, 100.0)
            command = self.local_output / 100.0
        availability_fault = self.faults.factor("cooling_water_availability", 1.0)
        target_availability = clamp(command * availability_fault, 0.0, 1.0)
        self.cooling_availability = first_order(self.cooling_availability, target_availability, 3.0, dt)

        vacuum_target = 1.0 if running else 0.0
        vacuum_target *= self.faults.factor("vacuum_system_availability", 1.0)
        self.vacuum_output = first_order(self.vacuum_output, vacuum_target,
                                         self.vacuum_pull_time / 4.0, dt)

        # --- 冷凝能力 ---
        self.capacity = self.rated_capacity * self.cooling_availability * self.heat_transfer_factor

        # --- 壓力 ---
        excess = max(0.0, self.exhaust_in - self.capacity)
        p_min = self.p_min_base + self.p_min_load * self.exhaust_in
        air_leak = self.faults.factor("air_leak_kg_s", 0.0)
        d_pressure = (
            self.k_overload * excess
            - self.k_vacuum * self.vacuum_output * (self.pressure - p_min)
            + self.air_leak_gain * air_leak
        )
        if self.vacuum_output < 0.05:
            # 真空系統停止 -> 壓力回到大氣
            d_pressure = 0.02 * (1.0 - self.pressure) + self.k_overload * excess
        self.pressure = clamp(self.pressure + d_pressure * dt, 0.005, 2.0)

        # --- 冷凝與熱井 ---
        self.condensed = min(self.exhaust_in, self.capacity)
        # 熱井水位本地控制：AUTO 模式由補水閥維持設定值（40011）
        if self.auto_mode:
            error = self.hr("SECONDARY_SETPOINT") - self.hotwell_level
            self.makeup_command = clamp(self.makeup_command + self.makeup_gain * error * dt,
                                        0.0, 100.0)
        else:
            self.makeup_command = clamp(self.hr("MAKEUP_VALVE_CMD"), 0.0, 100.0)
        self.makeup_flow = self.max_makeup * self.makeup_command / 100.0
        leak = self.faults.factor("hotwell_leak_kg_s", 0.0)
        self.hotwell_mass += (self.condensed + self.makeup_flow - self.condensate_out - leak) * dt
        self.hotwell_mass = clamp(self.hotwell_mass, 0.0, self.mass_max * 1.2)
        self.hotwell_level = self._level(self.hotwell_mass)
        self.condensate_temp = first_order(self.condensate_temp, saturation_temp_c(self.pressure),
                                           10.0, dt)

        if self.sm.state is DeviceState.STARTING and self.pressure <= 0.15:
            self.sm.to(DeviceState.RUNNING, "VACUUM_ESTABLISHED")
        elif self.sm.state is DeviceState.STOPPING and self.vacuum_output < 0.05:
            self.sm.to(DeviceState.OFF, "STOPPED")

        self.alarms.set(CODE + 14, self.cooling_availability < 0.8 and running,
                        self.cooling_availability * 100.0, 80.0)
        self.alarms.set(CODE + 15, running and self.vacuum_output < 0.5,
                        self.vacuum_output * 100.0, 50.0)
        self.mass_total += self.condensed * dt

    def apply_comm_loss(self, policy: str) -> None:
        # 維持本地冷卻控制：冷卻能力保持 100%
        if policy in ("LOCAL_FALLBACK", "LOCAL_AUTO"):
            self.set_hr("MANUAL_OUTPUT", 100.0)

    def protection_values(self) -> dict[str, float]:
        pressure, _ = self.sensor_sample("pressure", self.pressure)
        level, _ = self.sensor_sample("hotwell_level", self.hotwell_level)
        return {"pressure": pressure, "hotwell_level": level, "capacity": self.capacity,
                "exhaust": self.exhaust_in}

    def publish(self) -> dict[str, float]:
        pressure, _ = self.sensor_sample("pressure", self.pressure)
        level, _ = self.sensor_sample("hotwell_level", self.hotwell_level)
        return {
            "condenser.pressure_bar_abs": pressure,
            "condenser.hotwell_level_pct": level,
            "condenser.condensed_kg_s": self.condensed,
            "condenser.capacity_kg_s": self.capacity,
            "condenser.condensate_temp_c": self.condensate_temp,
            "condenser.hotwell_mass_kg": self.hotwell_mass,
        }

    def fill_registers(self, regs: list[int]) -> None:
        pressure, _ = self.sensor_sample("pressure", self.pressure)
        level, _ = self.sensor_sample("hotwell_level", self.hotwell_level)
        regs[9] = enc_u16(pressure, 10000)
        regs[10] = enc_u16(max(0.0, 101.325 - pressure * 100.0), 100)
        regs[11] = enc_u16(max(0.0, level), 100)
        regs[12] = enc_u16(self.exhaust_in, 100)
        regs[13] = enc_u16(self.capacity, 100)
        regs[14] = enc_u16(self.condensed, 100)
        regs[15] = enc_u16(self.condensate_out, 100)
        regs[16] = enc_u16(self.cooling_availability * 100.0, 100)
        regs[17] = enc_u16(self.vacuum_output * 100.0, 100)
        regs[18] = enc_i16(self.condensate_temp, 10)
        regs[19], regs[20] = enc_u32(self.hotwell_mass)
        regs[21] = enc_u16(self.makeup_command, 100)
        regs[22] = enc_u16(self.makeup_flow, 100)


if __name__ == "__main__":
    run_device(Condenser)
