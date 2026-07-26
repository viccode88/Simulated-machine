"""各設備物理模型的方向性與守恆檢查。"""
from __future__ import annotations

import pytest

from common.modbus.register_map import DeviceState, Table
from common.simbus.protocol import SignalValue
from tests.harness import CONFIG_DIR, DEVICE_CLASSES


def build(name, tmp_path):
    device = DEVICE_CLASSES[name](config_dir=CONFIG_DIR, state_dir=str(tmp_path / name))
    device.bus_ok = True
    return device


def drive(device, inputs: dict, ticks: int = 1, dt: float = 0.1):
    for _ in range(ticks):
        device.inputs = {k: SignalValue(v, "GOOD", device.tick, "test") for k, v in inputs.items()}
        offset = device.rmap.offset_of(Table.HOLDING, "WATCHDOG_COUNTER")
        device.hold[offset] = (device.hold[offset] % 60000) + 1
        device.tick += 1
        device.sim_time += dt
        device.scan(dt)
    return device.publish()


# ------------------------------------------------------------------ 鍋爐
def test_boiler_mass_balance(tmp_path):
    boiler = build("boiler", tmp_path)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.flame = 2
    boiler.set_hr("BLOWDOWN_VALVE_CMD", 0.0)
    boiler.set_hr("MANUAL_OUTPUT", 0.0)
    start = boiler.water_mass
    drive(boiler, {"feedwater_pump.flow_kg_s": 50.0, "steam_valve.steam_flow_kg_s": 0.0}, ticks=100)
    # 100 個 tick × 0.1 s × 50 kg/s = 500 kg（燃燒器 0 -> 蒸發≈0）
    assert abs((boiler.water_mass - start) - 500.0) < 25.0


def test_boiler_pressure_direction(tmp_path):
    boiler = build("boiler", tmp_path)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.flame = 2
    boiler.pressure = 100.0
    boiler.evaporation = 80.0
    boiler.set_hr("MANUAL_OUTPUT", 80.0)
    drive(boiler, {"feedwater_pump.flow_kg_s": 80.0, "steam_valve.steam_flow_kg_s": 20.0}, ticks=20)
    assert boiler.pressure > 100.0, "蒸發量大於流出量時壓力必須上升"

    boiler.pressure = 100.0
    boiler.set_hr("MANUAL_OUTPUT", 0.0)
    drive(boiler, {"feedwater_pump.flow_kg_s": 80.0, "steam_valve.steam_flow_kg_s": 120.0}, ticks=20)
    assert boiler.pressure < 100.0, "流出量大於蒸發量時壓力必須下降"


def test_boiler_swell_effect(tmp_path):
    boiler = build("boiler", tmp_path)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.flame = 2
    boiler.evaporation = 90.0
    drive(boiler, {"feedwater_pump.flow_kg_s": 30.0, "steam_valve.steam_flow_kg_s": 90.0}, ticks=1)
    assert boiler.level_indicated > boiler.level_actual, "蒸發量大於給水時顯示水位應偏高（脹）"


def test_boiler_low_low_level_trips_and_latches(tmp_path):
    boiler = build("boiler", tmp_path)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.flame = 2
    boiler.water_mass = boiler.mass_min + 0.15 * (boiler.mass_max - boiler.mass_min)
    drive(boiler, {"feedwater_pump.flow_kg_s": 0.0, "steam_valve.steam_flow_kg_s": 0.0}, ticks=60)
    assert boiler.sm.tripped and boiler.protection.any_latched
    assert boiler.protection.first_out.name == "LOW_LOW_LEVEL"
    assert boiler.burner_output == 0.0
    # 水位恢復後仍鎖存
    boiler.water_mass = boiler.mass_min + 0.667 * (boiler.mass_max - boiler.mass_min)
    drive(boiler, {"feedwater_pump.flow_kg_s": 0.0, "steam_valve.steam_flow_kg_s": 0.0}, ticks=200)
    assert boiler.protection.any_latched, "跳機不得因數值恢復自動清除"


def test_boiler_relief_valve_limits_overpressure(tmp_path):
    boiler = build("boiler", tmp_path)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.flame = 2
    boiler.pressure = 112.0
    boiler.evaporation = 20.0
    drive(boiler, {"feedwater_pump.flow_kg_s": 20.0, "steam_valve.steam_flow_kg_s": 0.0}, ticks=50)
    assert boiler.relief_flow > 0.0
    assert boiler.pressure < 115.0, "安全閥應在跳機門檻前抑制壓力"


# -------------------------------------------------------------- 主蒸汽閥
def test_valve_flow_scales_with_opening_and_pressure(tmp_path):
    valve = build("steam_valve", tmp_path)
    half = valve._flow(50.0, 100.0, 0.08, 500.0)
    full = valve._flow(100.0, 100.0, 0.08, 500.0)
    assert abs(full - 2 * half) < 1e-6
    assert abs(full - 120.0) < 1.0, "額定條件下全開流量應接近 120 kg/s"
    assert valve._flow(100.0, 50.0, 0.08, 500.0) < full


def test_valve_choked_vs_subcritical(tmp_path):
    valve = build("steam_valve", tmp_path)
    choked = valve._flow(100.0, 10.0, 1.0, 300.0)        # r = 0.1 < 0.55
    subcritical = valve._flow(100.0, 10.0, 8.0, 300.0)   # r = 0.8 > 0.55
    assert subcritical < choked


def test_valve_fast_close_on_turbine_trip(tmp_path):
    valve = build("steam_valve", tmp_path)
    valve.sm.force(DeviceState.RUNNING, "test")
    valve.position = 80.0
    valve.set_hr("MANUAL_OUTPUT", 80.0)
    inputs = {"boiler.pressure_bar_abs": 100.0, "boiler.steam_temp_c": 500.0,
              "condenser.pressure_bar_abs": 0.08, "turbine.tripped": 1.0, "boiler.tripped": 0.0,
              "turbine.speed_rpm": 3300.0}
    drive(valve, inputs, ticks=6)  # 0.6 秒
    assert valve.position <= 1.0, "跳機快關應在 0.3～0.5 秒內完成"
    assert valve.fast_close == 1


def test_valve_stuck_open_reports_fail_to_close(tmp_path):
    valve = build("steam_valve", tmp_path)
    valve.sm.force(DeviceState.RUNNING, "test")
    valve.position = 80.0
    valve.faults.enabled = True
    valve.faults.set("actuator", "valve_mode", "STUCK_OPEN")
    valve.set_hr("MANUAL_OUTPUT", 0.0)
    inputs = {"boiler.pressure_bar_abs": 100.0, "boiler.steam_temp_c": 500.0,
              "condenser.pressure_bar_abs": 0.08, "turbine.tripped": 0.0, "boiler.tripped": 0.0,
              "turbine.speed_rpm": 3000.0}
    drive(valve, inputs, ticks=60)
    assert valve.position >= 79.0
    assert valve.fail_to_close_flag == 1.0
    assert valve.protection.any_latched


# ---------------------------------------------------------------- 汽輪機
def test_turbine_speed_falls_on_load_increase(tmp_path):
    turbine = build("turbine", tmp_path)
    turbine.omega = 3000.0 / (60 / (2 * 3.141592653589793))
    turbine.sm.force(DeviceState.RUNNING, "test")
    inputs = {"steam_valve.steam_flow_kg_s": 60.0, "boiler.pressure_bar_abs": 100.0,
              "boiler.steam_temp_c": 500.0, "condenser.pressure_bar_abs": 0.08,
              "generator.electrical_power_mw": 60.0}
    drive(turbine, inputs, ticks=20)
    before = turbine.speed_rpm
    drive(turbine, {**inputs, "generator.electrical_power_mw": 90.0}, ticks=20)
    assert turbine.speed_rpm < before, "負載增加時轉速應先下降"


def test_turbine_overspeed_on_load_rejection(tmp_path):
    turbine = build("turbine", tmp_path)
    turbine.omega = 3000.0 / (60 / (2 * 3.141592653589793))
    turbine.sm.force(DeviceState.RUNNING, "test")
    inputs = {"steam_valve.steam_flow_kg_s": 90.0, "boiler.pressure_bar_abs": 100.0,
              "boiler.steam_temp_c": 500.0, "condenser.pressure_bar_abs": 0.08,
              "generator.electrical_power_mw": 0.0}
    drive(turbine, inputs, ticks=30)   # 甩載但閥門卡住
    assert turbine.speed_rpm > 3300.0
    assert turbine.protection.any_latched
    assert turbine.protection.first_out.name == "OVERSPEED"


def test_turbine_efficiency_drops_with_condenser_pressure(tmp_path):
    turbine = build("turbine", tmp_path)
    turbine.sm.force(DeviceState.RUNNING, "test")
    base = {"steam_valve.steam_flow_kg_s": 60.0, "boiler.pressure_bar_abs": 100.0,
            "boiler.steam_temp_c": 500.0, "generator.electrical_power_mw": 60.0}
    drive(turbine, {**base, "condenser.pressure_bar_abs": 0.08}, ticks=5)
    good = turbine.mech_power
    drive(turbine, {**base, "condenser.pressure_bar_abs": 0.25}, ticks=5)
    assert turbine.mech_power < good


# ---------------------------------------------------------------- 冷凝器
def test_condenser_pressure_rises_when_overloaded(tmp_path):
    condenser = build("condenser", tmp_path)
    condenser.sm.force(DeviceState.RUNNING, "test")
    condenser.set_hr("MANUAL_OUTPUT", 100.0)
    drive(condenser, {"turbine.exhaust_flow_kg_s": 60.0, "condensate_pump.flow_kg_s": 60.0},
          ticks=1800)
    stable = condenser.pressure
    assert stable < 0.12
    condenser.faults.enabled = True
    condenser.faults.set("process", "cooling_water_availability", 0.3)
    drive(condenser, {"turbine.exhaust_flow_kg_s": 60.0, "condensate_pump.flow_kg_s": 60.0},
          ticks=300)
    assert condenser.pressure > 0.25, "排汽超過冷凝能力時真空應快速惡化"


def test_hotwell_level_follows_flow_balance(tmp_path):
    condenser = build("condenser", tmp_path)
    condenser.sm.force(DeviceState.RUNNING, "test")
    condenser.set_hr("CONTROL_MODE", 0)   # LOCAL_MANUAL：關閉自動補水
    condenser.set_hr("MAKEUP_VALVE_CMD", 0.0)
    condenser.set_hr("MANUAL_OUTPUT", 100.0)
    condenser.cooling_availability = 1.0
    start = condenser.hotwell_level
    drive(condenser, {"turbine.exhaust_flow_kg_s": 0.0, "condensate_pump.flow_kg_s": 50.0},
          ticks=100)
    assert condenser.hotwell_level < start, "凝結水泵抽走而無排汽時熱井水位下降"


# ------------------------------------------------------------------ 泵浦
def test_feedwater_pump_flow_drops_with_boiler_pressure(tmp_path):
    pump = build("feedwater_pump", tmp_path)
    pump.sm.force(DeviceState.RUNNING, "test")
    pump.set_hr("MANUAL_OUTPUT", 100.0)
    pump.set_hr("OUTLET_VALVE_CMD", 100.0)
    inputs = {"feedwater_tank.level_pct": 60.0, "feedwater_tank.pressure_bar_abs": 4.8,
              "boiler.pressure_bar_abs": 80.0, "boiler.feedwater_permitted": 1.0}
    drive(pump, inputs, ticks=200)
    low_pressure_flow = pump.flow
    drive(pump, {**inputs, "boiler.pressure_bar_abs": 120.0}, ticks=200)
    assert pump.flow < low_pressure_flow, "相同轉速下鍋爐壓力上升，流量下降"


def test_pump_cavitates_at_low_source_level(tmp_path):
    pump = build("condensate_pump", tmp_path)
    pump.sm.force(DeviceState.RUNNING, "test")
    pump.set_hr("MANUAL_OUTPUT", 80.0)
    inputs = {"condenser.hotwell_level_pct": 60.0, "condenser.pressure_bar_abs": 0.08,
              "feedwater_tank.level_pct": 60.0, "feedwater_tank.pressure_bar_abs": 4.8}
    drive(pump, inputs, ticks=200)
    healthy = pump.flow
    assert pump.cavitation_factor > 0.99
    drive(pump, {**inputs, "condenser.hotwell_level_pct": 8.0}, ticks=100)
    assert pump.cavitation_factor < 0.3
    assert pump.flow < healthy * 0.5


def test_pump_trips_after_prolonged_cavitation(tmp_path):
    pump = build("condensate_pump", tmp_path)
    pump.sm.force(DeviceState.RUNNING, "test")
    pump.set_hr("MANUAL_OUTPUT", 80.0)
    inputs = {"condenser.hotwell_level_pct": 12.0, "condenser.pressure_bar_abs": 0.08,
              "feedwater_tank.level_pct": 60.0, "feedwater_tank.pressure_bar_abs": 4.8}
    drive(pump, inputs, ticks=400)
    assert pump.protection.any_latched
    assert pump.protection.first_out.name in ("CAVITATION_TRIP", "LOW_LOW_SUCTION")


def test_outlet_valve_closed_reduces_flow(tmp_path):
    pump = build("feedwater_pump", tmp_path)
    pump.sm.force(DeviceState.RUNNING, "test")
    pump.set_hr("MANUAL_OUTPUT", 100.0)
    inputs = {"feedwater_tank.level_pct": 60.0, "feedwater_tank.pressure_bar_abs": 4.8,
              "boiler.pressure_bar_abs": 80.0, "boiler.feedwater_permitted": 1.0}
    drive(pump, inputs, ticks=200)
    open_flow = pump.flow
    pump.set_hr("OUTLET_VALVE_CMD", 0.0)
    drive(pump, inputs, ticks=50)
    assert pump.flow < 0.01 * max(open_flow, 1.0) + 0.5


# -------------------------------------------------------------- 給水槽
def test_tank_level_follows_balance(tmp_path):
    tank = build("feedwater_tank", tmp_path)
    start = tank.level
    drive(tank, {"condensate_pump.flow_kg_s": 80.0, "feedwater_pump.flow_kg_s": 50.0,
                 "condenser.condensate_temp_c": 45.0}, ticks=100)
    assert tank.level > start
    mid = tank.level
    drive(tank, {"condensate_pump.flow_kg_s": 20.0, "feedwater_pump.flow_kg_s": 80.0,
                 "condenser.condensate_temp_c": 45.0}, ticks=100)
    assert tank.level < mid


# ------------------------------------------------------------------ 發電機
def test_breaker_close_requires_sync_permissives(tmp_path):
    generator = build("generator", tmp_path)
    generator.sm.force(DeviceState.RUNNING, "test")
    generator.coil[generator.rmap.offset_of(Table.COIL, "BREAKER_CLOSE")] = True
    drive(generator, {"turbine.speed_rpm": 1500.0, "turbine.mechanical_power_mw": 0.0,
                      "turbine.tripped": 0.0}, ticks=2)
    assert generator.breaker_closed == 0, "轉速不符時不可閉合斷路器"


def test_breaker_opens_on_turbine_trip(tmp_path):
    generator = build("generator", tmp_path)
    generator.sm.force(DeviceState.RUNNING, "test")
    generator.breaker_closed = 1
    generator.electrical_power = 60.0
    drive(generator, {"turbine.speed_rpm": 3000.0, "turbine.mechanical_power_mw": 60.0,
                      "turbine.tripped": 1.0}, ticks=5)
    assert generator.breaker_closed == 0
    assert generator.electrical_power < 60.0
