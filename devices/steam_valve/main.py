"""主蒸汽閥設備容器。

閥門動態  dPos/dt = clamp(Command - Actual, -CloseRate, OpenRate)
可壓縮流  r = Pdown / Pup
          r <= rcrit -> FlowFactor = 1
          否則        -> FlowFactor = sqrt((1-r)/(1-rcrit))
          M = Krated × Opening × Pup/Prated × sqrt(Tref/Tsteam) × FlowFactor
"""
from __future__ import annotations

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.modbus.encoding import enc_i16, enc_u16
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp

CODE = 5800

FAULT_MODES = (
    "STUCK_OPEN", "STUCK_CLOSED", "STUCK_POSITION", "SLOW_TRAVEL",
    "POSITION_FEEDBACK_BIAS", "FAIL_TO_CLOSE", "ACTUATOR_POWER_LOSS",
)


class SteamValve(BaseDevice):
    NAME = "steam_valve"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "FAIL_CLOSE"

    PROCESS_INPUTS = [
        RegSpec(9, "COMMAND_POSITION", "%", 100),
        RegSpec(10, "ACTUAL_POSITION", "%", 100),
        RegSpec(11, "UPSTREAM_PRESSURE", "bar(a)", 100),
        RegSpec(12, "DOWNSTREAM_PRESSURE", "bar(a)", 10000),
        RegSpec(13, "STEAM_FLOW", "kg/s", 100),
        RegSpec(14, "STEAM_TEMPERATURE", "degC", 10, dtype="i16"),
        RegSpec(15, "ACTUATOR_SPEED", "%/s", 100),
        RegSpec(16, "POSITION_DEVIATION", "%", 100, dtype="i16"),
        RegSpec(17, "FAST_CLOSE_STATUS", dtype="enum", desc="0=正常 1=快關中 2=快關完成"),
        RegSpec(18, "VALVE_FAULT_CODE", dtype="enum"),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "RPM", 1, lo=0, hi=4000, writable=True, desc="轉速設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="手動閥門位置"),
        RegSpec(29, "OPEN_RATE", "%/s", 100, lo=0.1, hi=500, writable=True, desc="正常開啟速度"),
        RegSpec(30, "CLOSE_RATE", "%/s", 100, lo=0.1, hi=500, writable=True, desc="正常關閉速度"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "POSITION_DEVIATION", 0, "閥門位置偏差"),
        AlarmSpec(CODE + 12, "FAST_CLOSE_ACTIVE", 1, "主蒸汽閥快關動作"),
        AlarmSpec(CODE + 13, "ACTUATOR_FAULT", 2, "執行器故障"),
        AlarmSpec(CODE + 14, "FAIL_TO_CLOSE", 3, "閥門無法關閉"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "POSITION_DEVIATION_TRIP", "signal": "deviation",
         "direction": "high", "alarm_code": CODE + 11, "message": "Steam valve position deviation"},
        {"code": CODE + 2, "name": "FAIL_TO_CLOSE", "signal": "fail_to_close", "direction": "high",
         "alarm_code": CODE + 14, "message": "Steam valve failed to close on demand"},
    ]

    PUBLISHES = ["steam_valve.steam_flow_kg_s", "steam_valve.position_pct",
                 "steam_valve.fast_close", "steam_valve.steam_temp_c"]
    SUBSCRIBES = ["boiler.pressure_bar_abs", "boiler.steam_temp_c", "condenser.pressure_bar_abs",
                  "turbine.tripped", "boiler.tripped", "turbine.speed_rpm"]

    STATE_VARS = ["position", "command", "fast_close", "fast_close_state", "actuator_speed",
                  "steam_flow", "deviation", "fail_to_close_flag", "close_demand_timer",
                  "feedback_bias", "upstream", "downstream", "steam_temp"]

    def configure(self) -> None:
        c = self.cfg.get("steam_valve", {})
        self.k_rated = float(c.get("rated_flow_kg_s", 120.0))
        self.p_rated = float(c.get("rated_pressure_bar", 100.0))
        self.t_reference_k = float(c.get("reference_temp_c", 500.0)) + 273.15
        self.r_critical = float(c.get("critical_ratio", 0.55))
        self.open_time = float(c.get("open_time_s", 5.0))
        self.close_time = float(c.get("close_time_s", 3.0))
        self.fast_close_time = float(c.get("fast_close_time_s", 0.4))
        self.deviation_limit = float(c.get("deviation_alarm_pct", 5.0))
        self.fail_to_close_delay = float(c.get("fail_to_close_delay_s", 3.0))
        self.local_gain = float(c.get("local_speed_gain", 0.02))

        self.position = 0.0
        self.command = 0.0
        self.fast_close = 0
        self.fast_close_state = 0
        self.actuator_speed = 0.0
        self.steam_flow = 0.0
        self.deviation = 0.0
        self.fail_to_close_flag = 0.0
        self.close_demand_timer = 0.0
        self.feedback_bias = 0.0
        self.upstream = 1.0
        self.downstream = 0.08
        self.steam_temp = 25.0

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "turbine.speed_setpoint_rpm", 3000.0)),
            "MANUAL_OUTPUT": 0.0,
            "OPEN_RATE": 100.0 / float(cfg_get(self.cfg, "steam_valve.open_time_s", 5.0)),
            "CLOSE_RATE": 100.0 / float(cfg_get(self.cfg, "steam_valve.close_time_s", 3.0)),
        }

    def control_output(self) -> float:
        return self.position

    def start_permissives(self) -> list[tuple[str, bool]]:
        return [
            ("無閥門跳機鎖存", not self.protection.any_latched),
            ("緊急停止未啟動", not self.estop),
            ("執行器電源正常", not self.faults.actuator("ACTUATOR_POWER_LOSS")),
        ]

    def on_start(self) -> None:
        self.sm.to(DeviceState.RUNNING, "START")

    def on_stop(self) -> None:
        self.set_hr("MANUAL_OUTPUT", 0.0)
        self.sm.to(DeviceState.STOPPING, "STOP")

    def on_trip(self, codes: list[int]) -> None:
        self.fast_close = 1

    # -- 物理 --------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.upstream = max(0.05, self.sig("boiler.pressure_bar_abs", 1.0))
        self.downstream = max(0.001, self.sig("condenser.pressure_bar_abs", 0.08))
        self.steam_temp = self.sig("boiler.steam_temp_c", 25.0)

        turbine_trip = self.sig("turbine.tripped", 0.0) >= 0.5
        boiler_trip = self.sig("boiler.tripped", 0.0) >= 0.5
        demand_fast_close = (
            turbine_trip or boiler_trip or self.sm.tripped or self.estop or self.force_safe
        )
        if demand_fast_close and not self.fast_close:
            self._emit("FAST_CLOSE_DEMAND", turbine_trip=turbine_trip, boiler_trip=boiler_trip,
                       position=round(self.position, 2))
        self.fast_close = 1 if demand_fast_close else 0
        self.alarms.set(CODE + 12, bool(self.fast_close))

        # --- 位置命令 ---
        if self.fast_close:
            self.command = 0.0
        elif self.sm.in_any([DeviceState.OFF, DeviceState.STOPPING]):
            self.command = 0.0
        elif self.watchdog_ok or self.comm_policy != "LOCAL_FALLBACK":
            self.command = clamp(self.hr("MANUAL_OUTPUT"), 0.0, 100.0)
        else:
            # 本地降階：以轉速偏差做簡單比例控制
            error = self.hr("PRIMARY_SETPOINT") - self.sig("turbine.speed_rpm", 0.0)
            self.command = clamp(self.position + self.local_gain * error * dt * 10.0, 0.0, 100.0)

        # --- 執行器 ---
        open_rate = max(0.1, self.hr("OPEN_RATE"))
        close_rate = max(0.1, self.hr("CLOSE_RATE"))
        if self.fast_close:
            close_rate = 100.0 / max(0.05, self.fast_close_time)

        fault = self.faults.actuator("valve_mode")
        if fault == "SLOW_TRAVEL":
            factor = float(self.faults.actuator("slow_factor", 0.2) or 0.2)
            open_rate *= factor
            close_rate *= factor
        elif fault == "POSITION_FEEDBACK_BIAS":
            self.feedback_bias = float(self.faults.actuator("bias_pct", 5.0) or 5.0)
        elif fault == "ACTUATOR_POWER_LOSS":
            # 失效位置 FAIL_CLOSE
            open_rate = 0.0
            close_rate = 100.0 / max(0.5, self.close_time)
            self.command = 0.0

        previous = self.position
        if fault == "STUCK_OPEN":
            self.position = max(self.position, float(self.faults.actuator("stuck_pct", 100.0) or 100.0))
        elif fault == "STUCK_CLOSED":
            self.position = 0.0
        elif fault in ("STUCK_POSITION", "FAIL_TO_CLOSE"):
            if fault == "FAIL_TO_CLOSE" and self.command > self.position:
                self.position = clamp(self.position + open_rate * dt, 0.0, 100.0)
            # 否則維持原位置
        else:
            delta = self.command - self.position
            step = clamp(delta, -close_rate * dt, open_rate * dt)
            self.position = clamp(self.position + step, 0.0, 100.0)
        self.actuator_speed = (self.position - previous) / max(dt, 1e-6)

        indicated_position = clamp(self.position + self.feedback_bias, 0.0, 110.0)
        self.deviation = indicated_position - self.command
        self.alarms.set(CODE + 11, abs(self.deviation) > self.deviation_limit,
                        self.deviation, self.deviation_limit)
        self.alarms.set(CODE + 13, bool(fault))

        # --- 無法關閉偵測 ---
        if self.command <= 1.0 and self.position > 5.0:
            self.close_demand_timer += dt
        else:
            self.close_demand_timer = 0.0
        self.fail_to_close_flag = 1.0 if self.close_demand_timer >= self.fail_to_close_delay else 0.0
        if self.fail_to_close_flag and not self.alarms.states[CODE + 14].active:
            self._emit("VALVE_FAIL_TO_CLOSE", position=round(self.position, 2),
                       command=round(self.command, 2))
        self.alarms.set(CODE + 14, bool(self.fail_to_close_flag))

        if self.fast_close:
            self.fast_close_state = 2 if self.position <= 1.0 else 1
        else:
            self.fast_close_state = 0

        # --- 流量 ---
        self.steam_flow = self._flow(self.position, self.upstream, self.downstream, self.steam_temp)
        self.mass_total += self.steam_flow * dt

        if self.sm.state is DeviceState.STOPPING and self.position <= 0.5:
            self.sm.to(DeviceState.OFF, "CLOSED")

    def _flow(self, opening_pct: float, upstream: float, downstream: float, temp_c: float) -> float:
        if opening_pct <= 0.0 or upstream <= 0.05:
            return 0.0
        ratio = clamp(downstream / upstream, 0.0, 1.0)
        if ratio <= self.r_critical:
            flow_factor = 1.0
        else:
            flow_factor = ((1.0 - ratio) / (1.0 - self.r_critical)) ** 0.5
        temp_k = max(50.0, temp_c + 273.15)
        return (
            self.k_rated
            * (opening_pct / 100.0)
            * (upstream / self.p_rated)
            * (self.t_reference_k / temp_k) ** 0.5
            * flow_factor
        )

    def apply_comm_loss(self, policy: str) -> None:
        if policy in ("FAIL_CLOSE", "FAIL_LOW"):
            self.set_hr("MANUAL_OUTPUT", 0.0)
            self.command = 0.0
        elif policy == "FAIL_OPEN":
            self.set_hr("MANUAL_OUTPUT", 100.0)

    def protection_values(self) -> dict[str, float]:
        return {
            "deviation": abs(self.deviation),
            "fail_to_close": self.fail_to_close_flag,
            "position": self.position,
            "flow": self.steam_flow,
        }

    def publish(self) -> dict[str, float]:
        return {
            "steam_valve.steam_flow_kg_s": self.steam_flow,
            "steam_valve.position_pct": self.position,
            "steam_valve.fast_close": float(self.fast_close),
            "steam_valve.steam_temp_c": self.steam_temp,
        }

    def fill_registers(self, regs: list[int]) -> None:
        position, _ = self.faults.sensor("position", self.position + self.feedback_bias, self.dt)
        regs[9] = enc_u16(self.command, 100)
        regs[10] = enc_u16(position, 100)
        regs[11] = enc_u16(self.upstream, 100)
        regs[12] = enc_u16(self.downstream, 10000)
        regs[13] = enc_u16(self.steam_flow, 100)
        regs[14] = enc_i16(self.steam_temp, 10)
        regs[15] = enc_u16(abs(self.actuator_speed), 100)
        regs[16] = enc_i16(self.deviation, 100)
        regs[17] = self.fast_close_state
        mode = self.faults.actuator("valve_mode")
        regs[18] = FAULT_MODES.index(mode) + 1 if mode in FAULT_MODES else 0


if __name__ == "__main__":
    run_device(SteamValve)
