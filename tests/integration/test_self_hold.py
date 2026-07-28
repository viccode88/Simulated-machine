"""自持行為驗收：沒有任何控制器，機組必須自己啟動並維持在可運行狀態。

這是本次改寫的核心契約：
* 環境條件（允許條件）成立就自行啟動
* 本地調節器把程序量維持在設定值
* SCADA 的 STOP 必須真的停得下來（不會下一拍又自己啟動）
* PLC／SCADA 全部離線也不影響運轉
"""
from __future__ import annotations

import pytest

from common.modbus.register_map import DeviceState, SelfHoldState, Table
from tests.harness import CONFIG_DIR, DEVICE_CLASSES, MiniPlant, run_until


@pytest.fixture(scope="module")
def cold_start():
    """一次冷啟動：完全不下任何命令，只推進時間。"""
    plant = MiniPlant()
    plant.run_seconds(1200.0, kick_watchdog=False)   # 連 PLC 的 watchdog 都沒有
    yield plant
    plant.close()


def test_plant_starts_itself_from_cold_without_any_controller(cold_start):
    plant = cold_start
    assert plant.signal("generator.electrical_power_mw") == pytest.approx(60.0, abs=3.0)
    assert plant.signal("turbine.speed_rpm") == pytest.approx(3000.0, abs=30.0)
    assert plant.signal("generator.breaker_closed") == 1.0
    assert not any(d.protection.any_latched for d in plant.devices.values()), "自持啟動不得跳機"


def test_every_device_reaches_its_own_setpoint(cold_start):
    plant = cold_start
    boiler = plant.dev("boiler")
    tank = plant.dev("feedwater_tank")
    condenser = plant.dev("condenser")
    assert boiler.pressure == pytest.approx(boiler.hr("PRIMARY_SETPOINT"), abs=6.0)
    assert boiler.level_indicated == pytest.approx(boiler.hr("SECONDARY_SETPOINT"), abs=5.0)
    assert tank.level == pytest.approx(tank.hr("PRIMARY_SETPOINT"), abs=8.0)
    assert condenser.hotwell_level == pytest.approx(condenser.hr("SECONDARY_SETPOINT"), abs=10.0)


def test_self_hold_state_and_local_output_are_readable(cold_start):
    """PLC／SCADA 不必猜：自持狀態與本地輸出都在 Input Register 上。"""
    plant = cold_start
    for name in ("boiler", "steam_valve", "feedwater_pump", "condenser"):
        device = plant.dev(name)
        offset = device.rmap.offset_of(Table.INPUT, "SELF_HOLD_STATE")
        assert device._image.inputs[offset] == int(SelfHoldState.RUNNING), name
        output = device.rmap.offset_of(Table.INPUT, "LOCAL_OUTPUT")
        assert device._image.inputs[output] > 0, f"{name} 本地調節器應有輸出"
        word = device.rmap.offset_of(Table.INPUT, "PERMISSIVE_WORD")
        assert device._image.inputs[word] > 0


def test_operator_stop_is_latched_and_start_releases_it(tmp_path):
    """SCADA 只做啟停：STOP 必須擋住自持啟動，START 才會放行。"""
    plant = MiniPlant(state_root=str(tmp_path))
    try:
        plant.run_seconds(120.0)
        condenser = plant.dev("condenser")
        assert condenser.sm.state in (DeviceState.STARTING, DeviceState.RUNNING)

        plant.pulse("condenser", "STOP")
        plant.run_seconds(60.0)
        assert condenser.operator_stop is True
        assert condenser.sm.state is DeviceState.OFF, "STOP 後不得自己又啟動"
        assert condenser.self_hold_state is SelfHoldState.OPERATOR_STOP

        plant.pulse("condenser", "START")
        plant.run_seconds(30.0)
        assert condenser.operator_stop is False
        assert condenser.sm.state in (DeviceState.STARTING, DeviceState.RUNNING)
    finally:
        plant.close()


def test_device_waits_for_its_permissives(tmp_path):
    """允許條件不成立時只能待機，不得硬啟動。"""
    plant = MiniPlant(state_root=str(tmp_path))
    try:
        valve = plant.dev("steam_valve")
        plant.run_seconds(20.0)
        # 冷態：鍋爐壓力遠低於最低進汽壓力
        assert valve.sm.state is DeviceState.OFF
        assert valve.self_hold_state is SelfHoldState.STANDBY
        blocked = [name for name, ok in valve.start_permissives() if not ok]
        assert "鍋爐壓力達最低進汽壓力" in blocked

        elapsed = run_until(plant, lambda p: p.dev("steam_valve").sm.running, timeout_s=600.0)
        assert elapsed > 0, "壓力足夠後主蒸汽閥必須自行開啟"
        assert valve.sig("boiler.pressure_bar_abs") >= valve.min_admission_pressure
    finally:
        plant.close()


def test_trip_blocks_self_start_until_reset(tmp_path):
    """跳機鎖存優先於自持：沒有重置就不得自己啟動。"""
    device = DEVICE_CLASSES["condenser"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "c"))
    try:
        device.bus_ok = True
        device.protection.force_trip(device.CODE_BASE + 99, 1.0, message="測試")
        device._trip("TEST")
        for _ in range(200):
            device.scan(0.1)
        assert device.sm.state is DeviceState.TRIPPED
        assert device.self_hold_state is SelfHoldState.TRIP_LOCKED
    finally:
        device.store.close()
        device.log.close()


def test_self_hold_can_be_disabled(tmp_path):
    """SELF_HOLD=false（或 control.self_hold: false）時退回純外部命令模式。"""
    device = DEVICE_CLASSES["condenser"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "d"))
    try:
        device.bus_ok = True
        device.self_hold = False
        for _ in range(200):
            device.scan(0.1)
        assert device.sm.state is DeviceState.OFF
        assert device.self_hold_state is SelfHoldState.DISABLED

        device.coil[device.rmap.offset_of(Table.COIL, "START")] = True
        device.scan(0.1)
        assert device.sm.state is not DeviceState.OFF, "外部 START 仍必須有效"
    finally:
        device.store.close()
        device.log.close()


def test_pumps_go_to_standby_instead_of_overfilling(tmp_path):
    """泵浦有最低轉速，因此下游滿了就必須真的停轉，而不是灌到溢流。"""
    plant = MiniPlant(state_root=str(tmp_path))
    try:
        pump = plant.dev("condensate_pump")
        tank = plant.dev("feedwater_tank")
        plant.run_seconds(600.0)
        assert tank.level < 90.0, f"給水槽不得被灌滿，實際 {tank.level:.1f}%"
        assert tank.overflow < 0.01, "不得溢流"
        assert pump.start_count >= 1
    finally:
        plant.close()
