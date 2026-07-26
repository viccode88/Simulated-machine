"""整廠整合測試：閉迴路啟動、快照快速恢復、跳機鎖存、持久化。"""
from __future__ import annotations

import copy
import os

import pytest

from common.modbus.register_map import RESET_KEY_VALUE, DeviceState, Table
from tests.harness import CONFIG_DIR, DEVICE_CLASSES, MiniPlant, bring_to_steady, simple_control


@pytest.fixture(scope="module")
def steady(tmp_path_factory):
    """建立一次穩態機組，之後所有測試都用快照回到這個起點（正是本專案新增的功能）。"""
    plant = MiniPlant(state_root=str(tmp_path_factory.mktemp("steady")))
    bring_to_steady(plant, load_mw=60.0, seconds=1200)
    snapshot = copy.deepcopy(plant.snapshot())
    yield plant, snapshot
    plant.close()


def restore(steady):
    plant, snapshot = steady
    plant.restore(copy.deepcopy(snapshot), {"clear_latches": True})
    return plant


# ------------------------------------------------------------------ 穩態
def test_steady_state_within_acceptance_ranges(steady):
    plant = restore(steady)
    assert 2900 <= plant.signal("turbine.speed_rpm") <= 3100
    assert 80 <= plant.signal("boiler.pressure_bar_abs") <= 110
    assert 45 <= plant.signal("boiler.level_pct") <= 80
    assert 0.04 <= plant.signal("condenser.pressure_bar_abs") <= 0.12
    assert 40 <= plant.signal("feedwater_tank.level_pct") <= 75
    steam = plant.signal("steam_valve.steam_flow_kg_s")
    feed = plant.signal("feedwater_pump.flow_kg_s")
    assert abs(steam - feed) < 8.0, "蒸汽與給水流量應大致平衡"
    assert not any(d.protection.any_latched for d in plant.devices.values())


def test_water_inventory_is_conserved(steady):
    plant = restore(steady)
    start = plant.inventory()
    for _ in range(200):
        simple_control(plant, 60.0)
        plant.step(5)
    # 只有排污、補水與溢流會改變總水量
    assert abs(plant.inventory() - start) < 4000


# ------------------------------------------------------------ 快照往返
def test_snapshot_restore_is_bit_exact(steady):
    plant = restore(steady)
    for _ in range(50):
        simple_control(plant, 60.0)
        plant.step(5)
    document = copy.deepcopy(plant.snapshot())
    before = {name: device.snapshot_state()["physics"] for name, device in plant.devices.items()}

    # 大幅擾動
    plant.write("generator", "PRIMARY_SETPOINT", 95.0)
    for _ in range(200):
        simple_control(plant, 95.0)
        plant.step(5)
    assert plant.signal("boiler.pressure_bar_abs") != pytest.approx(
        document["bus"]["signals"]["boiler.pressure_bar_abs"], abs=0.05)

    plant.restore(copy.deepcopy(document))
    after = {name: device.snapshot_state()["physics"] for name, device in plant.devices.items()}
    assert after == before, "還原後所有物理狀態必須與快照完全一致"
    assert plant.sim_time == document["bus"]["sim_time"]


def test_snapshot_restore_bumps_generation_counter(steady):
    plant = restore(steady)
    boiler = plant.dev("boiler")
    before = boiler.snapshot_generation
    plant.restore(copy.deepcopy(plant.snapshot()))
    assert boiler.snapshot_generation == (before + 1) & 0xFFFF
    offset = boiler.rmap.offset_of(Table.INPUT, "SNAPSHOT_GENERATION")
    assert boiler._image.inputs[offset] == boiler.snapshot_generation


def test_snapshot_keeps_trip_latch_and_clean_mode_clears_it(steady):
    plant = restore(steady)
    boiler = plant.dev("boiler")
    boiler.protection.force_trip(5199, plant.sim_time, message="測試用跳機")
    boiler._trip("TEST")
    plant.step(5)
    assert boiler.protection.any_latched and boiler.sm.tripped
    tripped_snapshot = copy.deepcopy(plant.snapshot())

    # 1) 忠實還原：鎖存與第一故障必須保留
    plant.restore(copy.deepcopy(tripped_snapshot))
    assert plant.dev("boiler").protection.any_latched
    assert plant.dev("boiler").protection.first_out_code() == 5199
    assert plant.dev("boiler").sm.state is DeviceState.TRIPPED

    # 2) clean 模式：得到未跳機的乾淨測試起點
    plant.restore(copy.deepcopy(tripped_snapshot), {"clear_latches": True})
    assert not plant.dev("boiler").protection.any_latched
    assert plant.dev("boiler").protection.first_out is None
    assert plant.dev("boiler").sm.state is not DeviceState.TRIPPED


def test_restore_discards_pending_commands(steady):
    plant = restore(steady)
    boiler = plant.dev("boiler")
    document = copy.deepcopy(plant.snapshot())
    from common.modbus.server import WriteRequest

    boiler._on_write(WriteRequest(Table.HOLDING, 11, [9999], "10.0.0.1", 1, 0x06))
    assert boiler._cmd_queue
    boiler.restore_state(document["participants"]["boiler"], {})
    assert not boiler._cmd_queue, "還原後不可套用舊命令"


# ------------------------------------------------------ 跳機鎖存與重置
def test_trip_survives_container_restart(tmp_path):
    """重新建立設備物件＝容器重啟：跳機鎖存與第一故障必須仍在。"""
    state_dir = str(tmp_path / "boiler")
    boiler = DEVICE_CLASSES["boiler"](config_dir=CONFIG_DIR, state_dir=state_dir)
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.protection.force_trip(5101, 10.0, value=18.0, message="低低水位")
    boiler._trip("TEST")
    boiler.store.save(boiler._persist_payload())
    trips_before = boiler.trip_count
    boiler.store.close()

    revived = DEVICE_CLASSES["boiler"](config_dir=CONFIG_DIR, state_dir=state_dir)
    assert revived.protection.any_latched, "容器重啟後不得自動清除跳機"
    assert revived.protection.first_out_code() == 5101
    assert revived.sm.state is DeviceState.TRIPPED, "重啟後應回到安全狀態"
    assert revived.trip_count == trips_before
    assert revived.boot_count == 2
    revived.store.close()


def test_reset_requires_key_sequence_and_safe_conditions(tmp_path):
    boiler = DEVICE_CLASSES["boiler"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "b"))
    boiler.bus_ok = True
    boiler.sm.force(DeviceState.RUNNING, "test")
    boiler.protection.force_trip(5101, 10.0, value=18.0)
    boiler._trip("TEST")
    coil = boiler.rmap.offset_of(Table.COIL, "RESET_TRIP")

    # 1) 沒有 Reset Key -> 拒絕
    boiler.coil[coil] = True
    boiler.scan(0.1)
    assert boiler.protection.any_latched
    assert boiler.rejected_commands >= 1

    # 2) Key 正確但緊急停止中 -> 拒絕
    boiler.set_hr("RESET_KEY", RESET_KEY_VALUE)
    boiler.hold[boiler.rmap.offset_of(Table.HOLDING, "COMMAND_SEQUENCE")] = 7
    boiler.coil[boiler.rmap.offset_of(Table.COIL, "EMERGENCY_STOP")] = True
    boiler.coil[coil] = True
    boiler.scan(0.1)
    assert boiler.protection.any_latched

    # 3) 解除緊急停止並等待重置條件成立
    boiler.coil[boiler.rmap.offset_of(Table.COIL, "EMERGENCY_STOP")] = False
    for _ in range(120):
        boiler.scan(0.1)
    boiler.hold[boiler.rmap.offset_of(Table.HOLDING, "COMMAND_SEQUENCE")] = 8
    boiler.coil[coil] = True
    boiler.scan(0.1)
    assert not boiler.protection.any_latched
    assert boiler.sm.state is DeviceState.OFF

    # 4) 相同命令序號不可重複觸發
    boiler.protection.force_trip(5101, 20.0, value=18.0)
    boiler._trip("TEST")
    for _ in range(120):
        boiler.scan(0.1)
    boiler.coil[coil] = True
    boiler.scan(0.1)
    assert boiler.protection.any_latched, "命令序號未更新時必須拒絕"
    boiler.store.close()


def test_start_command_rejected_while_latched(tmp_path):
    pump = DEVICE_CLASSES["feedwater_pump"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "p"))
    pump.bus_ok = True
    pump.protection.force_trip(5701, 1.0)
    pump._trip("TEST")
    before = pump.rejected_commands
    pump.coil[pump.rmap.offset_of(Table.COIL, "START")] = True
    pump.scan(0.1)
    assert pump.rejected_commands > before
    assert pump.sm.state is DeviceState.TRIPPED
    pump.store.close()


# ------------------------------------------------------------ 通訊失效
def test_watchdog_loss_drives_burner_to_zero(steady):
    plant = restore(steady)
    boiler = plant.dev("boiler")
    boiler.set_hr("MANUAL_OUTPUT", 60.0)
    for _ in range(200):          # 20 秒不更新 watchdog
        plant.step(1, kick_watchdog=False)
    assert not boiler.watchdog_ok
    assert boiler.hr("MANUAL_OUTPUT") == 0.0, "通訊失效時燃燒器必須降為 0%"
    assert boiler.alarms.states[boiler.CODE_BASE + 91].active


def test_steam_valve_fails_closed_on_comm_loss(steady):
    plant = restore(steady)
    valve = plant.dev("steam_valve")
    assert valve.comm_policy == "FAIL_CLOSE"
    for _ in range(200):
        plant.step(1, kick_watchdog=False)
    assert valve.position < 5.0


# ------------------------------------------------------------ 情境行為
def test_load_rejection_causes_speed_rise_then_valve_closes(steady):
    plant = restore(steady)
    turbine = plant.dev("turbine")
    generator = plant.dev("generator")
    before = turbine.speed_rpm
    plant.pulse("generator", "BREAKER_OPEN")
    peak = before
    for _ in range(60):
        simple_control(plant, 60.0)
        plant.step(5)
        peak = max(peak, turbine.speed_rpm)
    assert generator.breaker_closed == 0
    assert peak > before, "甩載時轉速必須先上升"
    assert plant.dev("steam_valve").position < 30.0


def test_cooling_loss_trips_turbine_and_latch_persists(steady):
    plant = restore(steady)
    condenser = plant.dev("condenser")
    turbine = plant.dev("turbine")
    condenser.faults.enabled = True
    condenser.faults.set("process", "cooling_water_availability", 0.2)
    for _ in range(400):
        simple_control(plant, 60.0)
        plant.step(5)
        if turbine.protection.any_latched:
            break
    assert turbine.protection.any_latched
    assert turbine.protection.first_out.name == "LOW_VACUUM"
    assert plant.dev("generator").breaker_closed == 0
    # 冷卻恢復後跳機仍鎖存
    condenser.faults.clear("process", "cooling_water_availability")
    for _ in range(200):
        simple_control(plant, 60.0)
        plant.step(5)
    assert turbine.protection.any_latched, "跳機不因冷凝器壓力恢復而解除"


def test_feedwater_pump_trip_eventually_trips_boiler(steady):
    plant = restore(steady)
    pump = plant.dev("feedwater_pump")
    boiler = plant.dev("boiler")
    pump.faults.enabled = True
    pump.faults.set("actuator", "pump_trip", True)
    for _ in range(1200):
        simple_control(plant, 60.0)
        plant.step(5)
        if boiler.protection.any_latched:
            break
    assert pump.flow < 1.0
    assert boiler.protection.any_latched
    assert boiler.protection.first_out.name == "LOW_LOW_LEVEL"
    assert boiler.burner_output == 0.0
