"""鍋爐設備容器（自持）。

本地自持控制：水位與主蒸汽閥允許條件成立就自行吹掃、點火、升壓，
並由本地壓力調節器把汽包壓力維持在 40010 PRIMARY_SETPOINT。

水量平衡  dM/dt = Mfeedwater - Mevaporation - Mblowdown
水位      Level = 100 × (M - Mmin) / (Mmax - Mmin)
汽包脹縮  Level_indicated = Level_actual + Kswell × (Mevaporation - Mfeedwater)
蒸發      dMevap/dt = (Mevap_target - Mevap) / TauBoiler
壓力      dP/dt = Kpressure × (Mevap - Msteam_out) - Kloss × (P - Pambient)
"""
from __future__ import annotations

from common.device.alarm import AlarmSpec
from common.device.base_device import BaseDevice, run_device
from common.device.regulator import build_regulator
from common.device.state_machine import BOILER_TRANSITIONS
from common.modbus.encoding import enc_i16, enc_u16, enc_u32
from common.modbus.register_map import DeviceState, RegSpec
from common.util import cfg_get, clamp, first_order, ramp, rate_limit

CODE = 5100


class Boiler(BaseDevice):
    NAME = "boiler"
    CODE_BASE = CODE
    TRANSITIONS = BOILER_TRANSITIONS

    PROCESS_INPUTS = [
        RegSpec(9, "BOILER_PRESSURE", "bar(a)", 100, desc="鍋爐壓力"),
        RegSpec(10, "LEVEL_INDICATED", "%", 100, desc="顯示水位（含脹縮）"),
        RegSpec(11, "LEVEL_ACTUAL", "%", 100, desc="實際水位"),
        RegSpec(12, "FEEDWATER_FLOW", "kg/s", 100),
        RegSpec(13, "STEAM_GENERATION", "kg/s", 100),
        RegSpec(14, "STEAM_OUTFLOW", "kg/s", 100),
        RegSpec(15, "BURNER_OUTPUT", "%", 100),
        RegSpec(16, "STEAM_TEMPERATURE", "degC", 10, dtype="i16"),
        RegSpec(17, "WATER_MASS_HI", "kg", 1, dtype="u32"),
        RegSpec(18, "WATER_MASS_LO", "kg", 1),
        RegSpec(19, "FLAME_STATUS", dtype="enum", desc="0=無火焰 1=點火中 2=穩定"),
        RegSpec(20, "BLOWDOWN_FLOW", "kg/s", 100),
        RegSpec(21, "PURGE_TIME_REMAINING", "s", 10),
        RegSpec(22, "FEEDWATER_PERMITTED", dtype="enum"),
        RegSpec(23, "RELIEF_FLOW", "kg/s", 100, desc="安全閥排放量"),
    ]
    EXTRA_HOLDINGS = [
        RegSpec(9, "PRIMARY_SETPOINT", "bar(a)", 100, lo=0, hi=130, writable=True, desc="鍋爐壓力設定值"),
        RegSpec(10, "SECONDARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True, desc="鍋爐水位設定值"),
        RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True, desc="手動燃燒器輸出"),
        RegSpec(29, "BLOWDOWN_VALVE_CMD", "%", 100, lo=0, hi=100, writable=True, desc="排污閥命令"),
    ]

    ALARMS = [
        AlarmSpec(CODE + 11, "LOW_LEVEL", 0, "鍋爐水位低"),
        AlarmSpec(CODE + 12, "HIGH_LEVEL", 1, "鍋爐水位高"),
        AlarmSpec(CODE + 13, "HIGH_PRESSURE", 2, "鍋爐壓力高"),
        AlarmSpec(CODE + 14, "FLAME_UNSTABLE", 3, "火焰不穩"),
        AlarmSpec(CODE + 15, "FEEDWATER_MISMATCH", 4, "給水與蒸汽流量偏差過大"),
        AlarmSpec(CODE + 16, "LOW_PRESSURE", 5, "鍋爐壓力低"),
        AlarmSpec(CODE + 17, "RELIEF_VALVE_OPEN", 6, "安全閥動作"),
    ]
    PROTECTION_DEFS = [
        {"code": CODE + 1, "name": "LOW_LOW_LEVEL", "signal": "level_indicated", "direction": "low",
         "alarm_code": CODE + 11, "message": "Boiler low-low water level"},
        {"code": CODE + 2, "name": "HIGH_HIGH_LEVEL", "signal": "level_indicated", "direction": "high",
         "alarm_code": CODE + 12, "message": "Boiler high-high water level"},
        {"code": CODE + 3, "name": "HIGH_PRESSURE", "signal": "pressure", "direction": "high",
         "alarm_code": CODE + 13, "message": "Boiler overpressure"},
        {"code": CODE + 4, "name": "FLAME_FAILURE", "signal": "flame_fail", "direction": "high",
         "alarm_code": CODE + 14, "message": "Boiler flame failure"},
    ]

    PUBLISHES = [
        "boiler.pressure_bar_abs", "boiler.level_pct", "boiler.level_actual_pct",
        "boiler.steam_generation_kg_s", "boiler.steam_temp_c", "boiler.water_mass_kg",
        "boiler.tripped", "boiler.feedwater_permitted", "boiler.burner_output_pct",
    ]
    # generator.breaker_closed 是升壓限幅的解除條件：一旦併聯帶載，
    # 燃燒器被鎖在 20% 會讓壓力一路掉到低壓、轉速跟著垮
    SUBSCRIBES = ["feedwater_pump.flow_kg_s", "steam_valve.steam_flow_kg_s",
                  "generator.breaker_closed"]

    STATE_VARS = [
        "water_mass", "evaporation", "pressure", "steam_temp", "burner_output",
        "burner_command", "burner_demand", "flame", "purge_timer", "ignition_timer",
        "feedwater_permitted",
        "level_actual", "level_indicated", "blowdown_flow", "feedwater_flow", "steam_outflow",
        "flame_fail_flag", "relief_flow",
    ]

    # -- 設定 --------------------------------------------------------------
    def configure(self) -> None:
        c = self.cfg.get("boiler", {})
        self.mass_min = float(c.get("mass_min_kg", 20000.0))
        self.mass_max = float(c.get("mass_max_kg", 50000.0))
        self.rated_steam = float(c.get("rated_steam_kg_s", 100.0))
        self.tau_boiler = float(c.get("tau_boiler_s", 20.0))
        self.k_pressure = float(c.get("k_pressure", 0.02))
        self.k_loss = float(c.get("k_loss", 0.0002))
        self.p_ambient = float(c.get("ambient_pressure_bar", 1.0))
        self.k_swell = float(c.get("k_swell", 0.15))
        self.burner_up_rate = float(c.get("burner_up_rate_pct_s", 5.0))
        self.burner_down_rate = float(c.get("burner_down_rate_pct_s", 10.0))
        self.purge_time = float(c.get("purge_time_s", 30.0))
        self.ignition_time = float(c.get("ignition_time_s", 5.0))
        self.min_fire_pct = float(c.get("min_fire_pct", 12.0))
        self.min_turbine_pressure = float(c.get("min_turbine_pressure_bar", 30.0))
        self.superheat_max = float(c.get("superheat_max_c", 190.0))
        self.max_blowdown = float(c.get("max_blowdown_kg_s", 2.0))
        self.valve_close_permissive_pct = float(c.get("valve_close_permissive_pct", 5.0))
        self.relief_setpoint = float(c.get("relief_setpoint_bar", 113.0))
        self.relief_capacity = float(c.get("relief_capacity_kg_s", 40.0))

        # --- 本地自持控制：壓力 -> 燃燒器 ---
        ctl = c.get("control", {})
        # 鍋爐壓力是積分型程序（dP/dt ∝ 蒸發量），積分作用必須很小，
        # 否則會出現週期約 180 秒的振盪：壓力衝到安全閥、洩汽再把水位打亂。
        self.pressure_ctl = build_regulator(
            "boiler_pressure", ctl.get("pressure", {}),
            kp=1.5, ki=0.01, kd=3.0, out_min=0.0, out_max=100.0,
            rate_up=5.0, rate_down=10.0, deadband=0.2, integral_limit=100.0,
        )
        # 升壓期間限制燃燒器輸出，避免壓力大幅過衝；限幅要進到調節器內部，
        # 從外面 min() 會讓積分累積到上限，解除瞬間直接衝到 100% 而超壓跳機。
        self.startup_burner_max = float(ctl.get("startup_burner_max_pct", 20.0))
        # 汽輪機真的開始抽汽（或壓力已接近設定值）才解除升壓限幅：
        # 一旦帶載，燃燒器被鎖在 20% 會讓壓力一路掉到低壓、轉速跟著垮。
        self.ramp_release_steam = float(ctl.get("ramp_release_steam_kg_s", 25.0))
        self.pressure_ramp_done = False
        # 高壓 runback：甩載時蒸汽需求瞬間消失，正常降載速率（10 %/s）追不上
        # 蒸發的一階遲滯（tau 20 s），壓力會一路衝過安全閥到超壓跳機。
        # 壓力達高壓警報門檻就直接切燃料，不受正常速率限制——這正是真實機組
        # 「高壓 runback」的行為，也是自持設備維持自己在可運行狀態的一部分。
        self.runback_pressure = float(ctl.get(
            "runback_pressure_bar", cfg_get(self.cfg, "protections.HIGH_PRESSURE.alarm", 108.0)))
        self.runback_rate = float(ctl.get("runback_rate_pct_s", 100.0))
        self.pressure_runback = 0.0

        # 物理狀態
        self.water_mass = float(c.get("initial_mass_kg", self.mass_min + 0.667 * (self.mass_max - self.mass_min)))
        self.evaporation = 0.0
        self.pressure = float(c.get("initial_pressure_bar", 1.0))
        self.steam_temp = 25.0
        self.burner_output = 0.0
        self.burner_command = 0.0
        self.burner_demand = 0.0
        self.flame = 0
        self.purge_timer = 0.0
        self.ignition_timer = 0.0
        self.feedwater_permitted = 1
        self.level_actual = self._level(self.water_mass)
        self.level_indicated = self.level_actual
        self.blowdown_flow = 0.0
        self.feedwater_flow = 0.0
        self.steam_outflow = 0.0
        self.flame_fail_flag = 0.0
        self.relief_flow = 0.0
        self.trip_cause = 0

        self.set_inhibit("FLAME_FAILURE", lambda: self.sm.state in (
            DeviceState.OFF, DeviceState.PURGING, DeviceState.TRIPPED, DeviceState.MAINTENANCE))
        self.set_inhibit("LOW_LOW_LEVEL", lambda: self.sm.state in (
            DeviceState.OFF, DeviceState.MAINTENANCE))

    def default_holdings(self) -> dict[str, float]:
        return {
            "PRIMARY_SETPOINT": float(cfg_get(self.cfg, "boiler.pressure_setpoint_bar", 100.0)),
            "SECONDARY_SETPOINT": float(cfg_get(self.cfg, "boiler.level_setpoint_pct", 66.7)),
            "MANUAL_OUTPUT": 0.0,
            "BLOWDOWN_VALVE_CMD": float(cfg_get(self.cfg, "boiler.blowdown_default_pct", 5.0)),
        }

    # -- 輔助 --------------------------------------------------------------
    def _level(self, mass: float) -> float:
        return 100.0 * (mass - self.mass_min) / (self.mass_max - self.mass_min)

    @staticmethod
    def _saturation_temp(pressure_bar: float) -> float:
        """簡化飽和溫度關聯式：T_sat ≈ 100 × P^0.25（1 bar→100°C，100 bar→316°C）。"""
        return 100.0 * max(pressure_bar, 0.05) ** 0.25

    def control_output(self) -> float:
        return self.burner_output

    # -- 啟動允許條件（§7.1） ----------------------------------------------
    def start_permissives(self) -> list[tuple[str, bool]]:
        return [
            ("水位在 30%～80%", 30.0 <= self.level_indicated <= 80.0),
            ("給水泵可用", self.sig("feedwater_pump.flow_kg_s", 0.0) >= 0.0 and self.bus_ok),
            ("主蒸汽閥接近關閉", self.sig("steam_valve.steam_flow_kg_s", 0.0) <= 5.0),
            ("無鍋爐跳機鎖存", not self.protection.any_latched),
            ("模擬匯流排正常", self.bus_ok),
            ("緊急停止未啟動", not self.estop),
        ]

    def on_start(self) -> None:
        self.purge_timer = self.purge_time
        self.sm.to(DeviceState.PURGING, "START")

    def on_stop(self) -> None:
        self.burner_command = 0.0
        self.burner_demand = 0.0
        self.sm.to(DeviceState.STOPPING, "STOP")

    def on_reset(self) -> None:
        self.trip_cause = 0
        self.feedwater_permitted = 1
        self.flame_fail_flag = 0.0
        # 重置後重新走一次升壓限幅，避免帶著滿檔燃燒率重新點火
        self.pressure_ramp_done = False
        self.pressure_ctl.reset()

    def on_trip(self, codes: list[int]) -> None:
        # 燃燒器立即降為 0%、切斷燃料
        self.burner_command = 0.0
        self.burner_demand = 0.0
        self.burner_output = 0.0
        self.flame = 0
        self.pressure_ctl.hold(0.0)
        first = self.protection.first_out_code()
        self.trip_cause = first
        # 給水控制依跳機原因處理
        if first == CODE + 2:      # 高高水位 -> 停止給水
            self.feedwater_permitted = 0
        elif first == CODE + 3:    # 超壓 -> 停止燃燒並關閉主蒸汽
            self.feedwater_permitted = 1
        else:                       # 低低水位等 -> 允許補水，但不得自動重啟燃燒器
            self.feedwater_permitted = 1
        self._emit("BOILER_TRIP_ACTIONS", first_out=first,
                   feedwater_permitted=self.feedwater_permitted)

    # -- 物理 --------------------------------------------------------------
    def step(self, dt: float) -> None:
        state = self.sm.state

        # --- 狀態機時序 ---
        if state is DeviceState.PURGING:
            self.purge_timer = max(0.0, self.purge_timer - dt)
            if self.purge_timer <= 0.0:
                self.ignition_timer = self.ignition_time
                self.sm.to(DeviceState.IGNITING, "PURGE_COMPLETE")
        elif state is DeviceState.IGNITING:
            self.ignition_timer = max(0.0, self.ignition_timer - dt)
            self.flame = 1
            if self.ignition_timer <= 0.0:
                self.flame = 2
                self.sm.to(DeviceState.PRESSURIZING, "FLAME_ESTABLISHED")
        elif state is DeviceState.PRESSURIZING:
            if self.pressure >= self.min_turbine_pressure:
                self.sm.to(DeviceState.RUNNING, "MIN_PRESSURE_REACHED")
        elif state is DeviceState.STOPPING:
            if self.burner_output <= 0.1:
                self.flame = 0
                self.sm.to(DeviceState.OFF, "STOPPED")

        # --- 流量（本地控制要用，必須先於燃燒器計算） ---
        self.feedwater_flow = max(0.0, self.sig("feedwater_pump.flow_kg_s", 0.0))
        self.steam_outflow = max(0.0, self.sig("steam_valve.steam_flow_kg_s", 0.0))
        self.blowdown_flow = self.max_blowdown * self.hr("BLOWDOWN_VALVE_CMD") / 100.0
        leak = self.faults.factor("boiler_leak_kg_s", 0.0)

        # --- 燃燒器命令（本地壓力自持控制） ---
        firing_states = (DeviceState.IGNITING, DeviceState.PRESSURIZING, DeviceState.RUNNING)
        self.pressure_ctl.setpoint = self.hr("PRIMARY_SETPOINT")
        if self.sm.tripped or self.estop or self.force_safe or state not in firing_states:
            demand = 0.0
            self.pressure_ctl.hold(0.0)
            self.local_output = 0.0
        else:
            if (self.pressure >= self.pressure_ctl.setpoint * 0.95
                    or self.sig("generator.breaker_closed", 0.0) >= 0.5
                    or self.steam_outflow >= self.ramp_release_steam):
                self.pressure_ramp_done = True
            cap = 100.0 if self.pressure_ramp_done else self.startup_burner_max
            demand = self.local_demand(self.pressure_ctl, self.pressure, dt, out_max=cap)
            demand = clamp(demand, self.hr("OUTPUT_LOW_LIMIT"), self.hr("OUTPUT_HIGH_LIMIT"))
            demand = max(demand, self.min_fire_pct)
            if self.pressure >= self.runback_pressure:
                demand = 0.0
                self.pressure_ctl.hold(0.0)
                self.local_output = 0.0
        # burner_demand = 控制端「要求」的燃燒率；burner_command = 執行器實際輸出。
        # 兩者必須分開，火焰失效偵測才能比較「要求燃燒」與「實際有火」。
        self.burner_demand = demand
        target = demand
        # 執行器故障：燃燒器火焰喪失
        if self.faults.actuator("burner_flame_loss"):
            target = 0.0
            self.flame = 0
        self.burner_command = target
        runback = self.pressure >= self.runback_pressure and target < self.burner_output
        if runback and not self.pressure_runback:
            self._emit("PRESSURE_RUNBACK", pressure=round(self.pressure, 2),
                       threshold=self.runback_pressure, burner=round(self.burner_output, 1))
        self.pressure_runback = 1.0 if runback else 0.0
        self.burner_output = rate_limit(
            self.burner_output, target, self.burner_up_rate,
            self.runback_rate if runback else self.burner_down_rate, dt)
        if self.flame == 0 and self.burner_output > 0.0 and state in firing_states:
            self.burner_output = 0.0

        # --- 蒸發 ---
        availability = ramp(self.level_actual, 5.0, 20.0)
        evap_target = self.rated_steam * (self.burner_output / 100.0) * availability
        self.evaporation = first_order(self.evaporation, evap_target, self.tau_boiler, dt)

        # --- 水量平衡 ---
        self.water_mass += (self.feedwater_flow - self.evaporation - self.blowdown_flow - leak) * dt
        self.water_mass = clamp(self.water_mass, 0.0, self.mass_max * 1.2)
        self.level_actual = self._level(self.water_mass)

        # --- 汽包脹縮 ---
        swell = self.k_swell * (self.evaporation - self.feedwater_flow)
        self.level_indicated = clamp(self.level_actual + swell, -10.0, 130.0)

        # --- 安全閥（超壓保護的第一道防線，動作時會排掉蒸汽與水量） ---
        if self.pressure > self.relief_setpoint and not self.faults.actuator("relief_disabled"):
            self.relief_flow = self.relief_capacity * ramp(
                self.pressure, self.relief_setpoint, self.relief_setpoint + 2.0
            )
        else:
            self.relief_flow = 0.0

        # --- 壓力 ---
        d_pressure = (
            self.k_pressure * (self.evaporation - self.steam_outflow - self.relief_flow - leak)
            - self.k_loss * (self.pressure - self.p_ambient)
        )
        self.pressure = clamp(self.pressure + d_pressure * dt, 0.05, 300.0)
        # 安全閥排放的是已蒸發的蒸汽，水量在蒸發項已扣除，這裡只更新警報
        self.alarms.set(CODE + 17, self.relief_flow > 0.01, self.pressure, self.relief_setpoint)

        # --- 蒸汽溫度 ---
        sat = self._saturation_temp(self.pressure)
        superheat = self.superheat_max * (self.burner_output / 100.0) * ramp(self.pressure, 5.0, 60.0)
        self.steam_temp = first_order(self.steam_temp, sat + superheat, 20.0, dt)

        # --- 火焰失效偵測 ---
        # 必須以「要求燃燒」(burner_demand) 判斷，不能用執行器實際輸出：
        # burner_flame_loss 故障會把實際輸出壓成 0，若用它判斷則永遠測不到失效
        expect_flame = state in firing_states and self.burner_demand > 0.0
        self.flame_fail_flag = 1.0 if (expect_flame and self.flame == 0) else 0.0

        # --- 警報 ---
        mismatch = abs(self.feedwater_flow - self.steam_outflow)
        self.alarms.set(CODE + 15, self.sm.running and mismatch > 10.0, mismatch, 10.0)
        self.alarms.set(CODE + 16, self.sm.running and self.pressure < 80.0, self.pressure, 80.0)
        self.mass_total += self.evaporation * dt

    def apply_comm_loss(self, policy: str) -> None:
        # 自持設備預設 LOCAL_AUTO：本地壓力控制繼續運作，不因失去 PLC 而熄火。
        # 若設定成傳統失效政策，仍維持原本「燃燒器降至 0%」的行為。
        if policy in ("FAIL_LOW", "FAIL_CLOSE"):
            self.set_hr("MANUAL_OUTPUT", 0.0)
            self.pressure_ctl.hold(0.0)
            self.burner_command = 0.0
            self.burner_demand = 0.0

    def protection_values(self) -> dict[str, float]:
        return {
            "level_indicated": self.level_indicated,
            "level_actual": self.level_actual,
            "pressure": self.pressure,
            "flame_fail": self.flame_fail_flag,
            "steam_flow": self.steam_outflow,
            "feedwater_flow": self.feedwater_flow,
            "burner_output": self.burner_output,
        }

    def publish(self) -> dict[str, float]:
        pressure, _ = self.sensor_sample("pressure", self.pressure)
        level, _ = self.sensor_sample("level", self.level_indicated)
        return {
            "boiler.pressure_bar_abs": pressure,
            "boiler.level_pct": level,
            "boiler.level_actual_pct": self.level_actual,
            "boiler.steam_generation_kg_s": self.evaporation,
            "boiler.steam_temp_c": self.steam_temp,
            "boiler.water_mass_kg": self.water_mass,
            "boiler.tripped": 1.0 if (self.sm.tripped or self.protection.any_latched) else 0.0,
            "boiler.feedwater_permitted": float(self.feedwater_permitted),
            "boiler.burner_output_pct": self.burner_output,
        }

    def fill_registers(self, regs: list[int]) -> None:
        pressure, _ = self.sensor_sample("pressure", self.pressure)
        level, _ = self.sensor_sample("level", self.level_indicated)
        regs[9] = enc_u16(pressure, 100)
        regs[10] = enc_u16(max(0.0, level), 100)
        regs[11] = enc_u16(max(0.0, self.level_actual), 100)
        regs[12] = enc_u16(self.feedwater_flow, 100)
        regs[13] = enc_u16(self.evaporation, 100)
        regs[14] = enc_u16(self.steam_outflow, 100)
        regs[15] = enc_u16(self.burner_output, 100)
        regs[16] = enc_i16(self.steam_temp, 10)
        regs[17], regs[18] = enc_u32(self.water_mass)
        regs[19] = self.flame
        regs[20] = enc_u16(self.blowdown_flow, 100)
        regs[21] = enc_u16(self.purge_timer, 10)
        regs[22] = int(self.feedwater_permitted)
        regs[23] = enc_u16(self.relief_flow, 100)

    def snapshot_extra(self) -> dict:
        return {
            "trip_cause": self.trip_cause,
            "pressure_ramp_done": self.pressure_ramp_done,
            "pressure_runback": self.pressure_runback,
            "pressure_ctl": self.pressure_ctl.to_dict(),
        }

    def restore_extra(self, data: dict) -> None:
        self.trip_cause = int(data.get("trip_cause", 0))
        self.pressure_ramp_done = bool(data.get("pressure_ramp_done", False))
        self.pressure_runback = float(data.get("pressure_runback", 0.0))
        self.pressure_ctl.from_dict(data.get("pressure_ctl") or {})


if __name__ == "__main__":
    run_device(Boiler)
