"""汽輪機設備容器（自持）。

汽輪機本身沒有執行器（調速器閥門是獨立設備），自持行為＝真空與主蒸汽壓力
允許條件成立就自行進汽升速，轉速由主蒸汽閥的本地調速器維持。

機械功率 Pmech = Kturbine × Msteam × (Psteam/Prated)^0.2 × Efficiency
轉速     dω/dt = (Pmech - Pelec - D×(ω-ω0)) / EquivalentInertia
         RPM = ω × 60 / (2π)
排汽     Mexhaust ≈ Msteam（含小型內部蒸汽庫存）
"""
from __future__ import annotations

import math
import random

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.modbus.encoding import enc_i16, enc_u16
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp, first_order, ramp

CODE = 5200
RPM_PER_RAD_S = 60.0 / (2.0 * math.pi)


class Turbine(BaseDevice):
    NAME = "turbine"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "LOCAL_AUTO"

    PROCESS_INPUTS = [
        RegSpec(9, "SPEED_RPM", "RPM", 1),
        RegSpec(10, "MECHANICAL_POWER", "MW", 100),
        RegSpec(11, "STEAM_FLOW", "kg/s", 100),
        RegSpec(12, "EXHAUST_FLOW", "kg/s", 100),
        RegSpec(13, "MAIN_STEAM_PRESSURE", "bar(a)", 100),
        RegSpec(14, "EXHAUST_PRESSURE", "bar(a)", 10000),
        RegSpec(15, "VIBRATION", "mm/s", 100),
        RegSpec(16, "BEARING_TEMPERATURE", "degC", 10, dtype="i16"),
        RegSpec(17, "GOVERNOR_OUTPUT", "%", 100),
        RegSpec(18, "ACCELERATION", "RPM/s", 10, dtype="i16"),
        RegSpec(19, "EFFICIENCY", "%", 100),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "RPM", 1, lo=0, hi=4000, writable=True, desc="轉速設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="手動調速器輸出"),
        RegSpec(29, "INERTIA_PARAM", "MW*s2/rad", 100, lo=0.1, hi=650, writable=True),
        RegSpec(30, "DAMPING_PARAM", "MW/(rad/s)", 1000, lo=0, hi=65, writable=True),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "HIGH_SPEED", 0, "汽輪機轉速高"),
        AlarmSpec(CODE + 12, "HIGH_VIBRATION", 1, "汽輪機振動高"),
        AlarmSpec(CODE + 13, "LOW_VACUUM", 2, "排汽壓力高（真空不良）"),
        AlarmSpec(CODE + 14, "HIGH_BEARING_TEMP", 3, "軸承溫度高"),
        AlarmSpec(CODE + 15, "LOW_SPEED", 4, "汽輪機轉速低"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "OVERSPEED", "signal": "speed_rpm", "direction": "high",
         "alarm_code": CODE + 11, "message": "Turbine overspeed"},
        {"code": CODE + 2, "name": "HIGH_VIBRATION", "signal": "vibration", "direction": "high",
         "alarm_code": CODE + 12, "message": "Turbine high vibration"},
        {"code": CODE + 3, "name": "LOW_VACUUM", "signal": "exhaust_pressure", "direction": "high",
         "alarm_code": CODE + 13, "message": "Turbine low condenser vacuum"},
        {"code": CODE + 4, "name": "HIGH_BEARING_TEMP", "signal": "bearing_temp", "direction": "high",
         "alarm_code": CODE + 14, "message": "Turbine bearing temperature high"},
    ]

    PUBLISHES = ["turbine.speed_rpm", "turbine.mechanical_power_mw", "turbine.exhaust_flow_kg_s",
                 "turbine.tripped", "turbine.vibration_mm_s"]
    SUBSCRIBES = ["steam_valve.steam_flow_kg_s", "boiler.pressure_bar_abs", "boiler.steam_temp_c",
                  "condenser.pressure_bar_abs", "generator.electrical_power_mw",
                  "steam_valve.position_pct"]

    STATE_VARS = ["omega", "speed_rpm", "mech_power", "steam_flow", "exhaust_flow", "vibration",
                  "bearing_temp", "acceleration", "efficiency", "internal_steam", "electrical_power",
                  "exhaust_pressure", "main_steam_pressure"]

    def configure(self) -> None:
        c = self.cfg.get("turbine", {})
        self.k_turbine = float(c.get("k_turbine_mw_per_kg_s", 1.0))
        self.rated_pressure = float(c.get("rated_pressure_bar", 100.0))
        self.rated_speed = float(c.get("rated_speed_rpm", 3000.0))
        self.inertia = float(c.get("equivalent_inertia", 3.18))
        self.damping = float(c.get("damping", 0.02))
        self.exhaust_tau = float(c.get("exhaust_tau_s", 2.0))
        self.vacuum_design = float(c.get("design_exhaust_bar", 0.08))
        self.vacuum_penalty = float(c.get("vacuum_penalty_per_bar", 1.2))
        self.bearing_tau = float(c.get("bearing_tau_s", 60.0))
        self.vibration_base = float(c.get("vibration_base_mm_s", 1.2))
        self.min_admission_pressure = float(c.get("min_admission_pressure_bar", 10.0))

        self.omega = 0.0
        self.speed_rpm = 0.0
        self.mech_power = 0.0
        self.steam_flow = 0.0
        self.exhaust_flow = 0.0
        self.vibration = self.vibration_base
        self.bearing_temp = 30.0
        self.acceleration = 0.0
        self.efficiency = 1.0
        self.internal_steam = 0.0
        self.electrical_power = 0.0
        self.exhaust_pressure = self.vacuum_design
        self.main_steam_pressure = 1.0
        self._last_speed = 0.0

        self.set_inhibit("HIGH_VIBRATION", lambda: self.speed_rpm < 300.0)
        self.set_inhibit("HIGH_BEARING_TEMP", lambda: self.sm.state is DeviceState.OFF)
        self.set_inhibit("LOW_VACUUM", lambda: self.speed_rpm < 500.0)

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "turbine.speed_setpoint_rpm", 3000.0)),
            "MANUAL_OUTPUT": 0.0,
            "INERTIA_PARAM": float(cfg_get(self.cfg, "turbine.equivalent_inertia", 3.18)),
            "DAMPING_PARAM": float(cfg_get(self.cfg, "turbine.damping", 0.02)),
        }

    def control_output(self) -> float:
        # 汽輪機的「控制輸出」就是主蒸汽閥開度（調速器在閥門端）
        return self.sig("steam_valve.position_pct", 0.0)

    def start_permissives(self) -> list[tuple[str, bool]]:
        vacuum_ok = self.sig("condenser.pressure_bar_abs", 1.0) <= float(
            cfg_get(self.cfg, "turbine.start_max_exhaust_bar", 0.15)
        )
        return [
            ("冷凝器真空良好", vacuum_ok),
            ("主蒸汽壓力足夠", self.sig("boiler.pressure_bar_abs", 0.0) >= self.min_admission_pressure),
            ("無汽輪機跳機鎖存", not self.protection.any_latched),
            ("緊急停止未啟動", not self.estop),
        ]

    def on_start(self) -> None:
        self.sm.to(DeviceState.STARTING, "START")

    def on_stop(self) -> None:
        self.sm.to(DeviceState.STOPPING, "STOP")

    def on_trip(self, codes: list[int]) -> None:
        self._emit("TURBINE_TRIP_ACTIONS", first_out=self.protection.first_out_code(),
                   speed_rpm=round(self.speed_rpm, 1))

    # -- 物理 --------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.main_steam_pressure = max(0.05, self.sig("boiler.pressure_bar_abs", 1.0))
        self.exhaust_pressure = max(0.001, self.sig("condenser.pressure_bar_abs", self.vacuum_design))
        steam_in = max(0.0, self.sig("steam_valve.steam_flow_kg_s", 0.0))
        self.electrical_power = max(0.0, self.sig("generator.electrical_power_mw", 0.0))
        self.steam_flow = steam_in

        # --- 效率（冷凝器壓力升高 -> 效率下降） ---
        self.efficiency = clamp(
            1.0 - self.vacuum_penalty * (self.exhaust_pressure - self.vacuum_design), 0.15, 1.05
        )
        pressure_factor = (self.main_steam_pressure / self.rated_pressure) ** 0.2

        # --- 機械功率 ---
        self.mech_power = max(
            0.0, self.k_turbine * steam_in * pressure_factor * self.efficiency
        )
        if self.sm.tripped:
            # 跳機後蒸汽閥應快關；殘餘蒸汽仍會產生功率
            pass

        inertia = max(0.1, self.hr("INERTIA_PARAM"))
        damping = max(0.0, self.hr("DAMPING_PARAM"))
        omega_ref = self.rated_speed / RPM_PER_RAD_S

        net = self.mech_power - self.electrical_power - damping * (self.omega - omega_ref)
        # 停機且無蒸汽時，以摩擦讓轉速衰減至 0
        if self.mech_power <= 0.01 and self.electrical_power <= 0.01:
            net = -damping * self.omega - 0.5
        self.omega = max(0.0, self.omega + (net / inertia) * dt)
        previous_rpm = self.speed_rpm
        self.speed_rpm = self.omega * RPM_PER_RAD_S
        self.acceleration = (self.speed_rpm - previous_rpm) / max(dt, 1e-6)

        # --- 狀態機 ---
        if self.sm.state is DeviceState.STARTING and self.speed_rpm >= self.rated_speed * 0.98:
            self.sm.to(DeviceState.RUNNING, "AT_SPEED")
        elif self.sm.state is DeviceState.STOPPING and self.speed_rpm < 50.0:
            self.sm.to(DeviceState.OFF, "STOPPED")
        elif self.sm.state is DeviceState.OFF and self.speed_rpm > 100.0 and not self.sm.tripped:
            self.sm.to(DeviceState.STARTING, "STEAM_ADMITTED")

        # --- 排汽（內部蒸汽庫存） ---
        self.internal_steam = first_order(self.internal_steam, steam_in, self.exhaust_tau, dt)
        self.exhaust_flow = max(0.0, self.internal_steam)

        # --- 振動 ---
        speed_ratio = self.speed_rpm / self.rated_speed
        vibration = self.vibration_base * (0.3 + speed_ratio)
        vibration += 6.0 * max(0.0, speed_ratio - 1.03) ** 1.5 * 10.0
        vibration += 0.6 * ramp(abs(self.acceleration), 50.0, 400.0)
        vibration += float(self.faults.factor("turbine_vibration_add", 0.0))
        if speed_ratio > 0.05:
            vibration += random.uniform(-0.05, 0.05)
        self.vibration = first_order(self.vibration, max(0.0, vibration), 0.5, dt)

        # --- 軸承溫度 ---
        target_temp = 35.0 + 45.0 * speed_ratio + 0.25 * self.mech_power
        target_temp += float(self.faults.factor("bearing_temp_add", 0.0))
        self.bearing_temp = first_order(self.bearing_temp, target_temp, self.bearing_tau, dt)

        self.local_output = self.sig("steam_valve.position_pct", 0.0)
        self.alarms.set(CODE + 15, self.sm.running and self.speed_rpm < self.rated_speed * 0.95,
                        self.speed_rpm, self.rated_speed * 0.95)
        self.energy_total += self.mech_power * dt / 3.6  # MW·s -> kWh

    def apply_comm_loss(self, policy: str) -> None:
        # LOCAL_AUTO（預設）：調速器在主蒸汽閥本地執行，失去 PLC 不必減速
        if policy in ("TRIP", "LOCAL_AUTO", "HOLD_LAST"):
            return
        self.set_hr("MANUAL_OUTPUT", 0.0)

    def protection_values(self) -> dict[str, float]:
        speed, _ = self.sensor_sample("speed", self.speed_rpm)
        return {
            "speed_rpm": speed,
            "vibration": self.vibration,
            "exhaust_pressure": self.exhaust_pressure,
            "bearing_temp": self.bearing_temp,
            "mech_power": self.mech_power,
            "steam_flow": self.steam_flow,
        }

    def publish(self) -> dict[str, float]:
        speed, _ = self.sensor_sample("speed", self.speed_rpm)
        return {
            "turbine.speed_rpm": speed,
            "turbine.mechanical_power_mw": self.mech_power,
            "turbine.exhaust_flow_kg_s": self.exhaust_flow,
            "turbine.tripped": 1.0 if (self.sm.tripped or self.protection.any_latched) else 0.0,
            "turbine.vibration_mm_s": self.vibration,
        }

    def fill_registers(self, regs: list[int]) -> None:
        speed, _ = self.sensor_sample("speed", self.speed_rpm)
        regs[9] = enc_u16(speed, 1)
        regs[10] = enc_u16(self.mech_power, 100)
        regs[11] = enc_u16(self.steam_flow, 100)
        regs[12] = enc_u16(self.exhaust_flow, 100)
        regs[13] = enc_u16(self.main_steam_pressure, 100)
        regs[14] = enc_u16(self.exhaust_pressure, 10000)
        regs[15] = enc_u16(self.vibration, 100)
        regs[16] = enc_i16(self.bearing_temp, 10)
        regs[17] = enc_u16(self.sig("steam_valve.position_pct", 0.0), 100)
        regs[18] = enc_i16(clamp(self.acceleration, -3000, 3000), 10)
        regs[19] = enc_u16(self.efficiency * 100.0, 100)


if __name__ == "__main__":
    run_device(Turbine)
