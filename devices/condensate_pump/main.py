"""凝結水泵設備容器。"""
from __future__ import annotations

from common.device.alarm import AlarmSpec
from common.device.base_device import run_device
from common.modbus.register_map import RegSpec
from common.util import cfg_get
from devices.pump_base import PUMP_PROCESS_INPUTS, PumpDevice

CODE = 5500


class CondensatePump(PumpDevice):
    NAME = "condensate_pump"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "LOCAL_FALLBACK"

    PROCESS_INPUTS = PUMP_PROCESS_INPUTS
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True,
                desc="給水槽水位設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="手動泵浦速度"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "LOW_SUCTION_LEVEL", 0, "熱井水位低"),
        AlarmSpec(CODE + 12, "CAVITATION", 1, "泵浦汽蝕"),
        AlarmSpec(CODE + 13, "MOTOR_OVERCURRENT", 2, "馬達過流"),
        AlarmSpec(CODE + 14, "LOW_FLOW", 3, "流量低"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "LOW_LOW_SUCTION", "signal": "source_level", "direction": "low",
         "alarm_code": CODE + 11, "message": "Condensate pump low-low hotwell level"},
        {"code": CODE + 2, "name": "CAVITATION_TRIP", "signal": "cavitation_time",
         "direction": "high", "alarm_code": CODE + 12,
         "message": "Condensate pump prolonged cavitation"},
        {"code": CODE + 3, "name": "MOTOR_OVERCURRENT", "signal": "motor_current_pu",
         "direction": "high", "alarm_code": CODE + 13,
         "message": "Condensate pump motor overcurrent"},
    ]

    PUBLISHES = ["condensate_pump.flow_kg_s", "condensate_pump.speed_pct",
                 "condensate_pump.discharge_bar_abs"]
    SUBSCRIBES = ["condenser.hotwell_level_pct", "condenser.pressure_bar_abs",
                  "feedwater_tank.level_pct", "feedwater_tank.pressure_bar_abs"]

    def configure(self) -> None:
        self.configure_pump(self.cfg.get("condensate_pump", {}))
        self.set_inhibit("CAVITATION_TRIP", lambda: self.speed < 1.0)
        self.set_inhibit("MOTOR_OVERCURRENT", lambda: self.speed < 1.0)
        self.set_inhibit("LOW_LOW_SUCTION", lambda: self.sm.state.name == "OFF")

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "feedwater_tank.level_setpoint_pct", 60.0)),
            "MANUAL_OUTPUT": 0.0,
        }

    # -- 介面實作 ----------------------------------------------------------
    def suction_pressure(self) -> float:
        return self.sig("condenser.pressure_bar_abs", 0.08)

    def discharge_required(self) -> float:
        return self.sig("feedwater_tank.pressure_bar_abs", 1.0)

    def source_level(self) -> float:
        return self.sig("condenser.hotwell_level_pct", 0.0)

    def step(self, dt: float) -> None:
        self.step_pump(dt)
        self.alarms.set(CODE + 12, self.cavitation_factor < 0.95 and self.speed > 1.0,
                        self.cavitation_factor * 100.0, 95.0)
        self.alarms.set(CODE + 13, self.motor_current > self.rated_current * 1.1,
                        self.motor_current, self.rated_current * 1.1)
        self.alarms.set(CODE + 14, self.sm.running and self.flow < 5.0, self.flow, 5.0)

    def publish(self) -> dict[str, float]:
        flow, _ = self.sensor_sample("flow", self.flow)
        return {
            "condensate_pump.flow_kg_s": flow,
            "condensate_pump.speed_pct": self.speed,
            "condensate_pump.discharge_bar_abs": self.discharge,
        }

    def fill_registers(self, regs: list[int]) -> None:
        self.fill_pump_registers(regs)


if __name__ == "__main__":
    run_device(CondensatePump)
