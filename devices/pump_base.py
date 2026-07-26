"""泵浦共通模型（凝結水泵、給水泵）。

PumpHead = ShutoffHead × Speed²
Flow     = RatedFlow × Speed × sqrt(max(0, 1 - dP / PumpHead)) × CavitationFactor
"""
from __future__ import annotations

import random

from common.device.base_device import BaseDevice
from common.modbus.encoding import enc_u16
from common.modbus.register_map import DeviceState, RegSpec
from common.util import clamp, first_order, ramp, rate_limit

# 兩種泵浦共用的程序量映射（30010～30018）
PUMP_PROCESS_INPUTS = [
    RegSpec(9, "ACTUAL_SPEED", "%", 100),
    RegSpec(10, "FLOW", "kg/s", 100),
    RegSpec(11, "SUCTION_PRESSURE", "bar(a)", 1000),
    RegSpec(12, "DISCHARGE_PRESSURE", "bar(a)", 100),
    RegSpec(13, "MOTOR_CURRENT", "A", 10),
    RegSpec(14, "NPSH_MARGIN", "m", 100),
    RegSpec(15, "VIBRATION", "mm/s", 100),
    RegSpec(16, "SOURCE_LEVEL", "%", 100),
    RegSpec(17, "BACKPRESSURE", "bar(a)", 100),
    RegSpec(18, "OUTLET_VALVE_POSITION", "%", 100),
    RegSpec(19, "CAVITATION_FACTOR", "%", 100),
]


class PumpDevice(BaseDevice):
    """泵浦共通行為。子類別需實作 suction_pressure / discharge_pressure / source_level。"""

    STATE_VARS = [
        "speed", "speed_command", "flow", "suction", "discharge", "motor_current",
        "npsh_margin", "vibration", "cavitation_factor", "cavitation_timer",
        "outlet_valve", "source_level_value", "backpressure",
    ]

    # -- 子類別提供 --------------------------------------------------------
    def suction_pressure(self) -> float:
        raise NotImplementedError

    def discharge_required(self) -> float:
        raise NotImplementedError

    def source_level(self) -> float:
        raise NotImplementedError

    def pump_flow_signal(self) -> str:
        raise NotImplementedError

    # -- 共通設定 ----------------------------------------------------------
    def configure_pump(self, c: dict) -> None:
        self.rated_flow = float(c.get("rated_flow_kg_s", 120.0))
        self.shutoff_head = float(c.get("shutoff_head_bar", 25.0))
        self.speed_rate = float(c.get("speed_rate_pct_s", 15.0))
        self.min_speed = float(c.get("min_speed_pct", 20.0))
        self.rated_current = float(c.get("rated_current_a", 250.0))
        self.cav_level_low = float(c.get("cavitation_level_low_pct", 15.0))
        self.cav_level_ok = float(c.get("cavitation_level_ok_pct", 25.0))
        self.cav_trip_time = float(c.get("cavitation_trip_time_s", 20.0))
        self.vibration_base = float(c.get("vibration_base_mm_s", 1.0))
        self.start_level_min = float(c.get("start_level_min_pct", 20.0))
        self.has_outlet_valve = bool(c.get("has_outlet_valve", False))
        # 排出壓力較高時，泵浦必須先達到能建立揚程的最低轉速（低於此值出口逆止閥不開）
        self.head_floor_enabled = bool(c.get("head_floor", True))
        self.head_floor_margin = float(c.get("head_floor_margin", 1.05))

        self.speed = 0.0
        self.speed_command = 0.0
        self.flow = 0.0
        self.suction = 1.0
        self.discharge = 1.0
        self.motor_current = 0.0
        self.npsh_margin = 5.0
        self.vibration = self.vibration_base
        self.cavitation_factor = 1.0
        self.cavitation_timer = 0.0
        self.outlet_valve = 100.0
        self.source_level_value = 0.0
        self.backpressure = 1.0

    def control_output(self) -> float:
        return self.speed

    def start_permissives(self) -> list[tuple[str, bool]]:
        return [
            ("來源水位高於最低值", self.source_level() >= self.start_level_min),
            ("吸入口壓力有效", self.suction_pressure() > 0.0),
            ("馬達可用", not self.faults.actuator("motor_unavailable")),
            ("排出路徑可用", self.outlet_valve > 1.0 or not self.has_outlet_valve),
            ("無跳機鎖存", not self.protection.any_latched),
            ("緊急停止未啟動", not self.estop),
        ]

    def on_start(self) -> None:
        self.sm.to(DeviceState.STARTING, "START")

    def on_stop(self) -> None:
        self.sm.to(DeviceState.STOPPING, "STOP")

    def on_trip(self, codes: list[int]) -> None:
        self.speed_command = 0.0

    # -- 共通物理 ----------------------------------------------------------
    def _head_floor_speed(self) -> float:
        """能建立排出揚程的最低轉速（%）。"""
        if not self.head_floor_enabled or self.shutoff_head <= 0:
            return 0.0
        differential = max(0.0, self.backpressure - self.suction)
        ratio = clamp(differential / self.shutoff_head, 0.0, 1.0)
        return clamp(100.0 * (ratio ** 0.5) * self.head_floor_margin, 0.0, 100.0)

    def step_pump(self, dt: float) -> None:
        self.source_level_value = self.source_level()
        self.suction = max(0.0, self.suction_pressure())
        self.backpressure = max(0.0, self.discharge_required())

        if self.has_outlet_valve:
            self.outlet_valve = clamp(self.hr("OUTLET_VALVE_CMD"), 0.0, 100.0)

        # --- 轉速命令 ---
        running_states = (DeviceState.STARTING, DeviceState.RUNNING)
        if self.sm.tripped or self.estop or self.force_safe or self.sm.state not in running_states:
            target = 0.0
        else:
            target = clamp(self.hr("MANUAL_OUTPUT"), 0.0, 100.0)
            target = clamp(target, self.hr("OUTPUT_LOW_LIMIT"), self.hr("OUTPUT_HIGH_LIMIT"))
            target = max(target, self.min_speed, self._head_floor_speed())
        derate = self.faults.factor("pump_derate", 1.0)
        target *= clamp(derate, 0.0, 1.0)
        if self.faults.actuator("pump_trip"):
            target = 0.0
        self.speed_command = target
        self.speed = rate_limit(self.speed, target, self.speed_rate, self.speed_rate * 2.0, dt)

        if self.sm.state is DeviceState.STARTING and self.speed >= self.min_speed * 0.95:
            self.sm.to(DeviceState.RUNNING, "AT_SPEED")
        elif self.sm.state is DeviceState.STOPPING and self.speed <= 0.5:
            self.sm.to(DeviceState.OFF, "STOPPED")

        # --- 汽蝕 ---
        target_cav = ramp(self.source_level_value, self.cav_level_low, self.cav_level_ok)
        self.cavitation_factor = first_order(self.cavitation_factor, target_cav, 1.0, dt)
        cavitating = self.cavitation_factor < 0.95 and self.speed > 1.0
        if cavitating:
            self.cavitation_timer += dt
        else:
            self.cavitation_timer = max(0.0, self.cavitation_timer - dt * 0.5)

        # --- 流量 ---
        speed_pu = self.speed / 100.0
        head = self.shutoff_head * speed_pu * speed_pu
        differential = self.backpressure - self.suction
        if head <= 1e-6 or speed_pu <= 1e-3:
            base_flow = 0.0
        else:
            base_flow = (
                self.rated_flow * speed_pu * max(0.0, 1.0 - differential / head) ** 0.5
            )
        valve_factor = (self.outlet_valve / 100.0) ** 0.5 if self.has_outlet_valve else 1.0
        flow = base_flow * self.cavitation_factor * valve_factor
        if cavitating:
            flow *= 1.0 + random.uniform(-0.15, 0.15)  # 汽蝕造成流量波動
        self.flow = max(0.0, flow)
        self.discharge = self.suction + head * (1.0 if self.flow <= 0.01 else
                                                clamp(1.0 - (self.flow / max(1.0, self.rated_flow)) ** 2, 0.1, 1.0))

        # --- 馬達電流、振動、NPSH ---
        current = self.rated_current * (0.2 + 0.8 * speed_pu) * (0.5 + 0.5 * self.flow / max(1.0, self.rated_flow))
        if cavitating:
            current *= 1.0 + random.uniform(-0.1, 0.15)
        current *= self.faults.factor("motor_current_factor", 1.0)
        self.motor_current = first_order(self.motor_current, current, 0.5, dt)

        vibration = self.vibration_base * (0.5 + speed_pu) + 6.0 * (1.0 - self.cavitation_factor)
        vibration += float(self.faults.factor("pump_vibration_add", 0.0))
        self.vibration = first_order(self.vibration, vibration, 1.0, dt)

        self.npsh_margin = clamp(
            (self.source_level_value / 100.0) * 8.0 + (self.suction - 0.05) * 10.0, -5.0, 30.0
        )
        self.mass_total += self.flow * dt

    def apply_comm_loss(self, policy: str) -> None:
        if policy == "HOLD_LAST":
            return
        if policy in ("FAIL_LOW", "FAIL_CLOSE"):
            self.set_hr("MANUAL_OUTPUT", 0.0)
        elif policy == "FAIL_HIGH":
            self.set_hr("MANUAL_OUTPUT", 100.0)

    def protection_values(self) -> dict[str, float]:
        return {
            "source_level": self.source_level_value,
            "cavitation_time": self.cavitation_timer,
            "motor_current_pu": self.motor_current / max(1.0, self.rated_current),
            "flow": self.flow,
            "vibration": self.vibration,
        }

    def fill_pump_registers(self, regs: list[int]) -> None:
        regs[9] = enc_u16(self.speed, 100)
        regs[10] = enc_u16(self.flow, 100)
        regs[11] = enc_u16(self.suction, 1000)
        regs[12] = enc_u16(self.discharge, 100)
        regs[13] = enc_u16(self.motor_current, 10)
        regs[14] = enc_u16(max(0.0, self.npsh_margin), 100)
        regs[15] = enc_u16(self.vibration, 100)
        regs[16] = enc_u16(max(0.0, self.source_level_value), 100)
        regs[17] = enc_u16(self.backpressure, 100)
        regs[18] = enc_u16(self.outlet_valve, 100)
        regs[19] = enc_u16(self.cavitation_factor * 100.0, 100)
