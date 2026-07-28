"""給水泵設備容器（自持）。

與凝結水泵同模型，但必須考慮較高排出壓力：
    PumpDischargePressure > BoilerPressure，否則即使泵浦旋轉也無法有效進水。

本地自持控制：三元素鍋爐水位控制（水位修正 + 蒸汽流量前饋 − 給水流量回授），
設定值取自 40010 PRIMARY_SETPOINT（預設 66.7%）。
"""
from __future__ import annotations

from common.device.alarm import AlarmSpec
from common.device.base_device import run_device
from common.device.regulator import build_regulator
from common.modbus.encoding import enc_u16
from common.modbus.register_map import RegSpec
from common.util import cfg_get, clamp
from devices.pump_base import PUMP_PROCESS_INPUTS, PumpDevice

CODE = 5700


class FeedwaterPump(PumpDevice):
    NAME = "feedwater_pump"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "LOCAL_AUTO"

    PROCESS_INPUTS = PUMP_PROCESS_INPUTS + [
        RegSpec(20, "BOILER_PRESSURE", "bar(a)", 100),
        RegSpec(21, "FEEDWATER_PERMITTED", dtype="enum"),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True, desc="鍋爐水位設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="手動泵浦速度"),
        RegSpec(29, "OUTLET_VALVE_CMD", "%", 100, lo=0, hi=100, writable=True, desc="手動出口閥位置"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "LOW_SUCTION_LEVEL", 0, "給水槽水位低"),
        AlarmSpec(CODE + 12, "CAVITATION", 1, "泵浦汽蝕"),
        AlarmSpec(CODE + 13, "MOTOR_OVERCURRENT", 2, "馬達過流"),
        AlarmSpec(CODE + 14, "NO_FLOW_HIGH_PRESSURE", 3, "排出壓力不足以進水"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "LOW_LOW_SUCTION", "signal": "source_level", "direction": "low",
         "alarm_code": CODE + 11, "message": "Feedwater pump low-low tank level"},
        {"code": CODE + 2, "name": "CAVITATION_TRIP", "signal": "cavitation_time",
         "direction": "high", "alarm_code": CODE + 12,
         "message": "Feedwater pump prolonged cavitation"},
        {"code": CODE + 3, "name": "MOTOR_OVERCURRENT", "signal": "motor_current_pu",
         "direction": "high", "alarm_code": CODE + 13,
         "message": "Feedwater pump motor overcurrent"},
    ]

    PUBLISHES = ["feedwater_pump.flow_kg_s", "feedwater_pump.speed_pct",
                 "feedwater_pump.discharge_bar_abs"]
    # boiler.level_pct 與 boiler.steam_generation_kg_s 是三元素控制的另外兩個元素
    SUBSCRIBES = ["feedwater_tank.level_pct", "feedwater_tank.pressure_bar_abs",
                  "boiler.pressure_bar_abs", "boiler.feedwater_permitted",
                  "boiler.level_pct", "boiler.steam_generation_kg_s"]

    def configure(self) -> None:
        config = dict(self.cfg.get("feedwater_pump", {}))
        config.setdefault("has_outlet_valve", True)
        self.configure_pump(config)
        # 三元素：水位迴路輸出流量修正（±50%），流量迴路才是轉速命令
        ctl = config.get("control", {})
        self.speed_ctl.out_min = -50.0
        self.speed_ctl.out_max = 50.0
        self.speed_ctl.kp = float(ctl.get("level", {}).get("kp", 1.5))
        self.speed_ctl.ki = float(ctl.get("level", {}).get("ki", 0.02))
        self.speed_ctl.deadband = float(ctl.get("level", {}).get("deadband", 0.3))
        self.speed_ctl.integral_limit = 50.0
        self.flow_ctl = build_regulator(
            "feedwater_flow", ctl.get("flow", {}),
            kp=1.2, ki=0.4, kd=0.0, out_min=0.0, out_max=100.0,
            rate_up=25.0, rate_down=25.0, integral_limit=100.0,
        )
        self.feedforward_gain = float(ctl.get("feedforward_gain", 1.0))
        self.set_inhibit("CAVITATION_TRIP", lambda: self.speed < 1.0)
        self.set_inhibit("MOTOR_OVERCURRENT", lambda: self.speed < 1.0)
        self.set_inhibit("LOW_LOW_SUCTION", lambda: self.sm.state.name == "OFF")

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "boiler.level_setpoint_pct", 66.7)),
            "MANUAL_OUTPUT": 0.0,
            "OUTLET_VALVE_CMD": 100.0,
        }

    # -- 介面實作 ----------------------------------------------------------
    def suction_pressure(self) -> float:
        return self.sig("feedwater_tank.pressure_bar_abs", 1.0)

    def discharge_required(self) -> float:
        return self.sig("boiler.pressure_bar_abs", 1.0)

    def source_level(self) -> float:
        return self.sig("feedwater_tank.level_pct", 0.0)

    def controlled_level(self) -> float:
        return self.sig("boiler.level_pct", 0.0)

    def local_speed_demand(self, dt: float) -> float:
        """三元素鍋爐水位控制。

        水位修正 + 蒸汽流量前饋 = 給水流量設定值，再由流量迴路決定轉速。
        單靠水位迴路會被汽包脹縮（swell）騙：升壓時水位假性上升，水位迴路
        反而減水，等 swell 消失就已經逼近低低水位。
        """
        self.speed_ctl.setpoint = self.level_setpoint()
        level_trim = self.local_demand(self.speed_ctl, self.controlled_level(), dt)
        steam = max(0.0, self.sig("boiler.steam_generation_kg_s", 0.0))
        feedforward = self.feedforward_gain * 100.0 * steam / max(1.0, self.rated_flow)
        self.flow_ctl.setpoint = clamp(level_trim + feedforward, 0.0, 150.0)
        measured = 100.0 * self.flow / max(1.0, self.rated_flow)
        if self.auto_mode:
            demand = self.flow_ctl.update(measured, dt)
        else:
            demand = clamp(self.hr("MANUAL_OUTPUT"), 0.0, 100.0)
            self.flow_ctl.track(demand)
        self.local_output = demand
        return demand

    def flow_inhibited(self) -> tuple[bool, str]:
        # 鍋爐高高水位跳機時禁止繼續補水：泵浦必須實際停轉，
        # 只把轉速命令歸零不夠，pump_base 的最低轉速／揚程下限會蓋掉它
        permitted = self.sig("boiler.feedwater_permitted", 1.0) >= 0.5
        return (not permitted), "鍋爐禁止給水（高高水位）"

    def on_trip(self, codes: list[int]) -> None:
        super().on_trip(codes)
        self.flow_ctl.hold(0.0)

    def snapshot_extra(self) -> dict:
        data = super().snapshot_extra()
        data["flow_ctl"] = self.flow_ctl.to_dict()
        return data

    def restore_extra(self, data: dict) -> None:
        super().restore_extra(data)
        self.flow_ctl.from_dict(data.get("flow_ctl") or {})

    def step(self, dt: float) -> None:
        self.step_pump(dt)
        self.alarms.set(CODE + 12, self.cavitation_factor < 0.95 and self.speed > 1.0,
                        self.cavitation_factor * 100.0, 95.0)
        self.alarms.set(CODE + 13, self.motor_current > self.rated_current * 1.1,
                        self.motor_current, self.rated_current * 1.1)
        self.alarms.set(
            CODE + 14,
            self.sm.running and self.speed > 50.0 and self.flow < 1.0,
            self.flow, 1.0,
        )

    def publish(self) -> dict[str, float]:
        flow, _ = self.sensor_sample("flow", self.flow)
        return {
            "feedwater_pump.flow_kg_s": flow,
            "feedwater_pump.speed_pct": self.speed,
            "feedwater_pump.discharge_bar_abs": self.discharge,
        }

    def fill_registers(self, regs: list[int]) -> None:
        self.fill_pump_registers(regs)
        regs[20] = enc_u16(self.sig("boiler.pressure_bar_abs", 1.0), 100)
        regs[21] = 1 if self.sig("boiler.feedwater_permitted", 1.0) >= 0.5 else 0


if __name__ == "__main__":
    run_device(FeedwaterPump)
