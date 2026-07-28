"""發電機設備容器（自持）。

本地自持控制：轉速接近額定就自行勵磁，同步條件成立就自行併聯（auto-sync），
併聯後把負載緩緩加到 40010 PRIMARY_SETPOINT（預設 60 MW）；鍋爐壓力不足時
自動暫停加載，避免對著軟掉的鍋爐拉負載造成壓力與轉速一起垮。

孤島模式（預設）：Pelectrical = LoadDemand，負載增加直接產生反向轉矩，
                  汽輪機轉速下降，調速器再開大蒸汽閥。
強電網模式：      斷路器閉合後轉速受電網頻率限制，蒸汽閥主要控制有功功率。
"""
from __future__ import annotations

import math

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.modbus.encoding import enc_i16, enc_u16
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp, first_order, rate_limit

CODE = 5300


class Generator(BaseDevice):
    NAME = "generator"
    CODE_BASE = CODE
    DEFAULT_COMM_POLICY = "LOCAL_AUTO"

    PROCESS_INPUTS = [
        RegSpec(9, "ELECTRICAL_POWER", "MW", 100),
        RegSpec(10, "LOAD_DEMAND", "MW", 100),
        RegSpec(11, "FREQUENCY", "Hz", 100),
        RegSpec(12, "VOLTAGE", "kV", 100),
        RegSpec(13, "CURRENT", "A", 1),
        RegSpec(14, "POWER_FACTOR", "", 1000),
        RegSpec(15, "BREAKER_STATUS", dtype="enum", desc="0=開路 1=閉合"),
        RegSpec(16, "SYNC_PERMISSIVE", dtype="bitfield"),
        RegSpec(17, "PHASE_ANGLE_DIFF", "deg", 100, dtype="i16"),
        RegSpec(18, "ELECTRICAL_TORQUE", "MW", 100),
        RegSpec(19, "OPERATING_MODE", dtype="enum", desc="0=孤島 1=強電網"),
    ]
    EXTRA_COILS = [
        RegSpec(9, "BREAKER_CLOSE", writable=True, pulse=True, desc="斷路器閉合命令"),
        RegSpec(10, "BREAKER_OPEN", writable=True, pulse=True, desc="斷路器打開命令"),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "MW", 100, lo=0, hi=200, writable=True, desc="負載設定"),
        RegSpec(10, "SECONDARY_SETPOINT", "kV", 100, lo=0, hi=100, writable=True, desc="電壓設定"),
        RegSpec(11, "MANUAL_OUTPUT", "MW", 100, lo=0, hi=200, writable=True, desc="手動負載命令"),
        RegSpec(29, "OPERATING_MODE", "", 1, lo=0, hi=1, writable=True, desc="0=孤島 1=強電網"),
        RegSpec(30, "LOAD_RATE_LIMIT", "MW/s", 100, lo=0.1, hi=200, writable=True),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "OVERCURRENT", 0, "發電機過流"),
        # 過頻與欠頻各自獨立：共用一個 code 會讓後評估的欠頻把過頻狀態覆寫掉
        AlarmSpec(CODE + 12, "OVERFREQUENCY", 1, "頻率過高"),
        AlarmSpec(CODE + 13, "REVERSE_POWER", 2, "逆功率"),
        AlarmSpec(CODE + 14, "SYNC_BLOCKED", 3, "同步條件不成立"),
        AlarmSpec(CODE + 15, "BREAKER_FAIL", 4, "斷路器拒動"),
        AlarmSpec(CODE + 16, "UNDERFREQUENCY", 5, "頻率過低"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "OVERCURRENT", "signal": "current_pu", "direction": "high",
         "alarm_code": CODE + 11, "message": "Generator overcurrent"},
        {"code": CODE + 2, "name": "OVERFREQUENCY", "signal": "frequency", "direction": "high",
         "alarm_code": CODE + 12, "message": "Generator overfrequency"},
        {"code": CODE + 3, "name": "UNDERFREQUENCY", "signal": "frequency", "direction": "low",
         "alarm_code": CODE + 16, "message": "Generator underfrequency"},
        {"code": CODE + 4, "name": "REVERSE_POWER", "signal": "reverse_power", "direction": "high",
         "alarm_code": CODE + 13, "message": "Generator reverse power"},
    ]

    PUBLISHES = ["generator.electrical_power_mw", "generator.breaker_closed",
                 "generator.frequency_hz", "generator.load_demand_mw",
                 "generator.operating_mode"]
    # boiler.pressure_bar_abs：加載允許條件（壓力不足時暫停加載）
    SUBSCRIBES = ["turbine.speed_rpm", "turbine.mechanical_power_mw", "turbine.tripped",
                  "boiler.pressure_bar_abs"]

    STATE_VARS = ["electrical_power", "load_demand", "voltage_kv", "frequency", "current_a",
                  "power_factor", "breaker_closed", "phase_angle", "operating_mode",
                  "speed_rpm", "reverse_power_flag", "breaker_fail_timer", "load_hold",
                  "auto_sync_blocked"]

    def configure(self) -> None:
        c = self.cfg.get("generator", {})
        self.rated_mw = float(c.get("rated_mw", 100.0))
        self.rated_kv = float(c.get("rated_kv", 15.75))
        self.rated_pf = float(c.get("rated_power_factor", 0.85))
        self.rated_speed = float(cfg_get(self.cfg, "turbine.rated_speed_rpm", 3000.0))
        self.grid_frequency = float(c.get("grid_frequency_hz", 50.0))
        self.grid_stiffness = float(c.get("grid_stiffness_mw_per_rpm", 2.0))
        self.load_tau = float(c.get("load_tau_s", 0.5))
        self.sync_speed_band = float(c.get("sync_speed_band_rpm", 30.0))
        self.sync_freq_band = tuple(c.get("sync_frequency_band_hz", [49.5, 50.5]))
        self.sync_voltage_band = tuple(c.get("sync_voltage_band_pct", [95.0, 105.0]))
        self.sync_angle_limit = float(c.get("sync_angle_limit_deg", 10.0))

        self.electrical_power = 0.0
        self.load_demand = 0.0
        self.voltage_kv = 0.0
        self.frequency = 0.0
        self.current_a = 0.0
        self.power_factor = self.rated_pf
        self.breaker_closed = 0
        self.phase_angle = 0.0
        self.operating_mode = 0
        self.speed_rpm = 0.0
        self.reverse_power_flag = 0.0
        self.breaker_fail_timer = 0.0
        self.sync_word = 0
        self.load_hold = 0.0        # 1 = 加載暫停中（鍋爐壓力不足）
        # 操作端（SCADA／PLC）主動開路後必須鎖住自動併聯，否則下一個掃描週期
        # 自動同步就會把斷路器再閉回去，操作員等於按不下去
        self.auto_sync_blocked = 0.0

        # --- 本地自持控制 ---
        ctl = c.get("control", {})
        self.auto_sync = bool(ctl.get("auto_sync", True))
        self.excite_speed_pct = float(ctl.get("excite_speed_pct", 90.0))
        # 併聯後才加載，且鍋爐壓力必須維持在設定值的這個比例以上
        self.load_pressure_ratio = float(ctl.get("load_min_pressure_ratio", 0.9))
        self.boiler_pressure_setpoint = float(
            cfg_get(self.cfg, "boiler.pressure_setpoint_bar", 100.0))
        self.sync_retry_timer = 0.0

        self.set_inhibit("OVERCURRENT", lambda: not self.breaker_closed)
        self.set_inhibit("REVERSE_POWER", lambda: not self.breaker_closed)
        # 過頻是電網側保護：斷路器打開時由汽輪機的超速保護（3300 RPM）負責，
        # 否則正常甩載（轉速短暫衝到 3130 RPM ≈ 52.2 Hz）就會留下一個沒有意義的
        # 跳機鎖存，自持機組會因此停在需要人工重置的狀態。
        self.set_inhibit("OVERFREQUENCY",
                         lambda: self.speed_rpm < 500.0 or not self.breaker_closed)
        self.set_inhibit("UNDERFREQUENCY", lambda: not self.breaker_closed)

    def default_holdings(self) -> dict[str, float]:
        return {
            # 自持機組的目標負載：SCADA 不必下設定值，開機就往這個負載走
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "generator.target_load_mw", 60.0)),
            "SECONDARY_SETPOINT": float(cfg_get(self.cfg, "generator.rated_kv", 15.75)),
            "MANUAL_OUTPUT": 0.0,
            "OPERATING_MODE": float(cfg_get(self.cfg, "generator.default_mode", 0)),
            "LOAD_RATE_LIMIT": float(cfg_get(self.cfg, "generator.load_rate_mw_s", 5.0)),
        }

    def control_output(self) -> float:
        return self.electrical_power

    # -- 同步允許 ----------------------------------------------------------
    def sync_permissives(self) -> list[tuple[str, bool]]:
        voltage_pct = 100.0 * self.voltage_kv / max(0.1, self.rated_kv)
        return [
            ("轉速接近額定", abs(self.speed_rpm - self.rated_speed) <= self.sync_speed_band),
            ("頻率在範圍內", self.sync_freq_band[0] <= self.frequency <= self.sync_freq_band[1]),
            ("電壓在範圍內", self.sync_voltage_band[0] <= voltage_pct <= self.sync_voltage_band[1]),
            ("相角差 < 限值", abs(self.phase_angle) <= self.sync_angle_limit),
            ("汽輪機未跳機", self.sig("turbine.tripped", 0.0) < 0.5),
            ("無電氣保護動作", not self.protection.any_latched),
        ]

    def start_permissives(self) -> list[tuple[str, bool]]:
        return [
            ("汽輪機未跳機", self.sig("turbine.tripped", 0.0) < 0.5),
            ("轉速接近額定（可勵磁）",
             self.sig("turbine.speed_rpm", 0.0) >= self.rated_speed * self.excite_speed_pct / 100.0),
            ("無跳機鎖存", not self.protection.any_latched),
            ("緊急停止未啟動", not self.estop),
        ]

    def on_start(self) -> None:
        # START 同時解除「操作端開路」的自動併聯鎖
        self.auto_sync_blocked = 0.0
        self.sm.to(DeviceState.RUNNING, "EXCITATION_ON")

    def on_stop(self) -> None:
        self._open_breaker("STOP")
        self.sm.to(DeviceState.OFF, "STOP")

    def on_trip(self, codes: list[int]) -> None:
        self._open_breaker("PROTECTION")

    def _open_breaker(self, reason: str) -> None:
        if not self.breaker_closed:
            return
        # 斷路器拒動故障：命令下達但接點不動作，斷路器仍保持閉合。
        # 這正是要模擬的危害，不能只點亮警報卻照常開路。
        if self.faults.actuator("breaker_fail_to_open"):
            self.alarms.set(CODE + 15, True, 1.0, 1.0)
            first = self.breaker_fail_timer <= 0.0
            self.breaker_fail_timer += self.dt
            if first:
                self._emit("BREAKER_FAIL_TO_OPEN", reason=reason,
                           power=round(self.electrical_power, 2))
            return
        self.breaker_fail_timer = 0.0
        self.breaker_closed = 0
        self.load_demand = 0.0
        if reason in ("MANUAL", "STOP", "FORCE_SAFE", "COMM_TIMEOUT"):
            self.auto_sync_blocked = 1.0
        self._emit("BREAKER_OPENED", reason=reason, auto_sync_blocked=self.auto_sync_blocked,
                   power_before=round(self.electrical_power, 2))

    def _close_breaker(self) -> None:
        blocked = [name for name, ok in self.sync_permissives() if not ok]
        if blocked:
            self.alarms.set(CODE + 14, True)
            self._reject("同步條件不成立", command="BREAKER_CLOSE", blocked=blocked)
            return
        if self.faults.actuator("breaker_fail_to_close"):
            self.alarms.set(CODE + 15, True)
            self._emit("BREAKER_FAIL_TO_CLOSE")
            return
        self.breaker_closed = 1
        self.alarms.set(CODE + 14, False)
        self._emit("BREAKER_CLOSED", speed_rpm=round(self.speed_rpm, 1),
                   frequency=round(self.frequency, 3), angle=round(self.phase_angle, 2))

    # -- 物理 --------------------------------------------------------------
    def step(self, dt: float) -> None:
        offset = self.rmap.offset_of
        from common.modbus.register_map import Table

        if self.coil[offset(Table.COIL, "BREAKER_CLOSE")]:
            self.coil[offset(Table.COIL, "BREAKER_CLOSE")] = False
            self._close_breaker()
        if self.coil[offset(Table.COIL, "BREAKER_OPEN")]:
            self.coil[offset(Table.COIL, "BREAKER_OPEN")] = False
            self._open_breaker("MANUAL")

        self.speed_rpm = max(0.0, self.sig("turbine.speed_rpm", 0.0))
        mech_power = max(0.0, self.sig("turbine.mechanical_power_mw", 0.0))
        turbine_tripped = self.sig("turbine.tripped", 0.0) >= 0.5
        self.operating_mode = int(round(self.hr("OPERATING_MODE")))

        self.frequency = self.speed_rpm / 60.0
        # 相角差：與模擬電網之間的滑差積分
        if not self.breaker_closed:
            self.phase_angle = (self.phase_angle + 360.0 * (self.frequency - self.grid_frequency) * dt)
            self.phase_angle = ((self.phase_angle + 180.0) % 360.0) - 180.0
        else:
            self.phase_angle = 0.0

        # --- 電壓（簡化 AVR） ---
        voltage_target = self.hr("SECONDARY_SETPOINT") if self.sm.running else 0.0
        voltage_target *= clamp(self.speed_rpm / self.rated_speed, 0.0, 1.05)
        self.voltage_kv = first_order(self.voltage_kv, voltage_target, 1.0, dt)

        # --- 負載需求 ---
        # FORCE_SAFE 是「強制安全狀態」線圈：對發電機而言安全狀態就是斷路器開路
        if turbine_tripped or self.sm.tripped or self.estop or self.force_safe:
            if turbine_tripped:
                reason = "TURBINE_TRIP"
            elif self.force_safe:
                reason = "FORCE_SAFE"
            else:
                reason = "TRIP"
            self._open_breaker(reason)
        # --- 自動同步（自持併聯） ---
        # 相角差持續滑移，同步窗口是一閃即逝的：每個掃描週期都重新判斷，
        # 條件成立就立刻閉合，不能只在某個時點試一次。
        if (self.auto_sync and self.auto_mode and self.sm.running and not self.breaker_closed
                and not self.auto_sync_blocked
                and not self.sm.tripped and not self.estop and not self.force_safe
                and not turbine_tripped and all(ok for _, ok in self.sync_permissives())):
            self._close_breaker()

        demand = self.hr("PRIMARY_SETPOINT") if self.auto_mode else self.hr("MANUAL_OUTPUT")
        demand += self.faults.factor("load_step_mw", 0.0)
        if not self.breaker_closed:
            demand = 0.0
        # 加載允許條件：鍋爐壓力不足時凍結負載，等鍋爐追上來再繼續加
        boiler_pressure = self.sig("boiler.pressure_bar_abs", self.boiler_pressure_setpoint)
        pressure_ok = boiler_pressure >= self.boiler_pressure_setpoint * self.load_pressure_ratio
        if self.auto_mode and self.breaker_closed and not pressure_ok:
            demand = min(demand, self.load_demand)
        hold = 1.0 if (self.breaker_closed and not pressure_ok) else 0.0
        if hold != self.load_hold:
            self._emit("LOAD_RAMP_HOLD" if hold else "LOAD_RAMP_RESUMED",
                       boiler_pressure=round(boiler_pressure, 2),
                       load_mw=round(self.load_demand, 2))
        self.load_hold = hold
        rate = max(0.1, self.hr("LOAD_RATE_LIMIT"))
        self.load_demand = rate_limit(self.load_demand, clamp(demand, 0.0, self.rated_mw * 1.5),
                                      rate, rate * 4.0, dt)

        # --- 電氣功率 ---
        if not self.breaker_closed:
            self.electrical_power = first_order(self.electrical_power, 0.0, 0.05, dt)
        elif self.operating_mode == 1:
            # 強電網：轉速被電網鎖定，電氣功率追隨機械功率
            stiff = self.grid_stiffness * (self.speed_rpm - self.rated_speed)
            self.electrical_power = clamp(mech_power + stiff, 0.0, self.rated_mw * 1.5)
        else:
            # 孤島：電氣功率 = 負載需求
            self.electrical_power = first_order(self.electrical_power, self.load_demand,
                                                self.load_tau, dt)

        # --- 電流、功因 ---
        self.power_factor = clamp(
            self.rated_pf + 0.1 * (1.0 - self.electrical_power / max(1.0, self.rated_mw)), 0.5, 1.0
        )
        if self.voltage_kv > 0.1:
            self.current_a = (self.electrical_power * 1e6) / (
                math.sqrt(3.0) * self.voltage_kv * 1000.0 * max(0.1, self.power_factor)
            )
        else:
            self.current_a = 0.0

        self.reverse_power_flag = 1.0 if (self.breaker_closed and mech_power < 0.5
                                          and self.electrical_power > 1.0) else 0.0
        self.local_output = self.load_demand

        self.energy_total += self.electrical_power * dt / 3.6  # MW·s -> kWh

    def apply_comm_loss(self, policy: str) -> None:
        # 保持負載短時間；嚴重逾時後打開斷路器
        if self.comm_loss_seconds > float(cfg_get(self.cfg, "comm.breaker_open_after_s", 10.0)):
            self._open_breaker("COMM_TIMEOUT")

    def protection_values(self) -> dict[str, float]:
        rated_current = (self.rated_mw * 1e6) / (
            math.sqrt(3.0) * self.rated_kv * 1000.0 * self.rated_pf
        )
        return {
            "current_pu": self.current_a / max(1.0, rated_current),
            "frequency": self.frequency,
            "reverse_power": self.reverse_power_flag,
            "power": self.electrical_power,
        }

    def publish(self) -> dict[str, float]:
        return {
            "generator.electrical_power_mw": self.electrical_power,
            "generator.breaker_closed": float(self.breaker_closed),
            "generator.frequency_hz": self.frequency,
            "generator.load_demand_mw": self.load_demand,
            "generator.operating_mode": float(self.operating_mode),
        }

    def fill_registers(self, regs: list[int]) -> None:
        from common.modbus.encoding import bits_to_word

        self.sync_word = bits_to_word([ok for _, ok in self.sync_permissives()])
        regs[9] = enc_u16(self.electrical_power, 100)
        regs[10] = enc_u16(self.load_demand, 100)
        regs[11] = enc_u16(self.frequency, 100)
        regs[12] = enc_u16(self.voltage_kv, 100)
        regs[13] = enc_u16(self.current_a, 1)
        regs[14] = enc_u16(self.power_factor, 1000)
        regs[15] = int(self.breaker_closed)
        regs[16] = self.sync_word
        regs[17] = enc_i16(self.phase_angle, 100)
        regs[18] = enc_u16(self.electrical_power, 100)
        regs[19] = int(self.operating_mode)


if __name__ == "__main__":
    run_device(Generator)
