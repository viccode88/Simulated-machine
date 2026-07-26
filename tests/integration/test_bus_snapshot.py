"""端對端測試：真正的 plant-bus + 設備（TCP simbus）+ HTTP API + 快照往返。

這條路徑涵蓋 lockstep 同步、品質管理、暫停/繼續、快照協調與故障注入路由，
不需要 docker。
"""
from __future__ import annotations

import asyncio
import json
import os

import aiohttp
import pytest

from common.util import EventLogger
from plant_bus.app.bus import PlantBus
from plant_bus.app.http_api import start_http
from plant_bus.app.snapshot_store import SnapshotStore
from tests.harness import CONFIG_DIR, DEVICE_CLASSES

BUS_PORT = 17100
HTTP_PORT = 18100
DEVICES = ["condenser", "condensate_pump", "feedwater_tank", "feedwater_pump", "boiler"]


async def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{HTTP_PORT}{path}"
    data = json.dumps(body or {}) if method in ("POST", "DELETE") else None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, data=data,
                                   headers={"Content-Type": "application/json"}) as response:
            return json.loads(await response.text())


@pytest.fixture
async def plant(tmp_path):
    cfg = {
        "simulation": {"dt": 0.1, "speed": 50.0, "tick_timeout": 1.0,
                       "bus_port": BUS_PORT, "devices": DEVICES},
        "bus": {"http_port": HTTP_PORT},
    }
    store = SnapshotStore(str(tmp_path / "snapshots"))
    log = EventLogger(device="plant-bus")
    bus = PlantBus(cfg, store, log)
    await bus.start()
    runner = await start_http(bus, port=HTTP_PORT)

    os.environ["SIM_BUS_HOST"] = "127.0.0.1"
    os.environ["SIM_BUS_PORT"] = str(BUS_PORT)
    os.environ["MODBUS_PORT"] = "0"          # 讓 OS 指派埠，避免測試互相衝突
    devices, tasks = {}, []
    for name in DEVICES:
        device = DEVICE_CLASSES[name](config_dir=CONFIG_DIR, state_dir=str(tmp_path / name))
        devices[name] = device
        tasks.append(asyncio.ensure_future(device.run()))
    # 等待全部設備完成 HELLO 並開始接收 tick
    for _ in range(100):
        await asyncio.sleep(0.05)
        if len(bus.participants) >= len(DEVICES) and bus.tick > 5:
            break
    yield bus, devices
    for device in devices.values():
        await device.shutdown()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bus.stop()
    await runner.cleanup()
    for key in ("SIM_BUS_HOST", "SIM_BUS_PORT", "MODBUS_PORT"):
        os.environ.pop(key, None)


async def test_all_devices_join_and_exchange_signals(plant):
    bus, devices = plant
    assert set(bus.participants) == set(DEVICES)
    state = await call("/state")
    assert state["tick"] > 0
    assert "boiler.pressure_bar_abs" in state["signals"]
    assert state["signals"]["boiler.pressure_bar_abs"]["quality"] == "GOOD"
    assert state["offline_devices"] == []
    assert not state["paused"]


async def test_pause_resume_and_step(plant):
    bus, _ = plant
    await call("/sim/pause", "POST")
    paused_tick = (await call("/state"))["tick"]
    await asyncio.sleep(0.3)
    assert (await call("/state"))["tick"] == paused_tick
    await call("/sim/step", "POST", {"ticks": 5})
    await asyncio.sleep(0.5)
    assert (await call("/state"))["tick"] == paused_tick + 5
    await call("/sim/resume", "POST")
    await asyncio.sleep(0.2)
    assert (await call("/state"))["tick"] > paused_tick + 5


async def test_snapshot_save_and_restore_round_trip(plant):
    bus, devices = plant
    boiler = devices["boiler"]

    # 建立一個有辨識度的狀態
    boiler.water_mass = 41234.0
    boiler.pressure = 55.5
    await asyncio.sleep(0.3)

    meta = await call("/snapshot/save", "POST", {"name": "baseline", "description": "測試基準"})
    assert meta["name"] == "baseline"
    assert set(meta["devices"]) == set(DEVICES), "所有設備都必須出現在快照中"
    assert not meta["missing"]
    assert bus.store.verify("baseline"), "checksum 必須正確"

    # 破壞狀態
    boiler.water_mass = 20500.0
    boiler.pressure = 5.0
    await asyncio.sleep(0.3)
    assert abs(boiler.water_mass - 41234.0) > 100

    generation_before = (await call("/state"))["snapshot_generation"]
    summary = await call("/snapshot/restore", "POST", {"name": "baseline"})
    assert set(summary["restored"]) == set(DEVICES)
    assert not summary["failed"] and not summary["missing"]
    assert summary["elapsed_ms"] < 5000

    assert abs(boiler.water_mass - 41234.0) < 50, "還原後物理量必須回到快照值"
    assert abs(boiler.pressure - 55.5) < 1.0
    state = await call("/state")
    assert state["snapshot_generation"] == generation_before + 1
    assert not state["paused"], "還原後應自動恢復執行"
    assert boiler.snapshot_generation > 0


async def test_restore_keeps_trip_latch_and_clean_mode_clears_it(plant):
    bus, devices = plant
    boiler = devices["boiler"]
    boiler.protection.force_trip(5199, boiler.sim_time, message="端對端測試")
    boiler._trip("TEST")
    await asyncio.sleep(0.3)
    await call("/snapshot/save", "POST", {"name": "tripped"})

    await call("/snapshot/restore", "POST", {"name": "tripped"})
    assert boiler.protection.any_latched
    assert boiler.protection.first_out_code() == 5199

    await call("/snapshot/restore", "POST", {"name": "tripped", "clear_latches": True})
    assert not boiler.protection.any_latched
    assert boiler.protection.first_out is None


async def test_snapshot_list_and_delete(plant):
    await call("/snapshot/save", "POST", {"name": "tmp1", "tags": ["x"]})
    listing = await call("/snapshot")
    assert any(m["name"] == "tmp1" for m in listing["snapshots"])
    assert (await call("/snapshot/tmp1", "DELETE"))["deleted"]
    listing = await call("/snapshot")
    assert not any(m["name"] == "tmp1" for m in listing["snapshots"])


async def test_fault_injection_routes_through_bus(plant):
    bus, devices = plant
    result = await call("/fault", "POST", {
        "target": "condenser", "category": "process",
        "name": "cooling_water_availability", "spec": 0.25,
    })
    assert result["acked"] == ["condenser"]
    await asyncio.sleep(0.2)
    assert devices["condenser"].faults.factor("cooling_water_availability", 1.0) == 0.25

    await call("/fault/clear", "POST", {"target": "condenser", "category": "process",
                                        "name": "cooling_water_availability"})
    await asyncio.sleep(0.2)
    assert devices["condenser"].faults.factor("cooling_water_availability", 1.0) == 1.0


async def test_signal_force_and_release(plant):
    await call("/signal/force", "POST", {"name": "boiler.pressure_bar_abs", "value": 42.0})
    await asyncio.sleep(0.3)
    state = await call("/state")
    assert state["signals"]["boiler.pressure_bar_abs"]["forced"]
    assert abs(state["signals"]["boiler.pressure_bar_abs"]["value"] - 42.0) < 0.01
    await call("/signal/force", "POST", {"name": "boiler.pressure_bar_abs", "value": None})
    await asyncio.sleep(0.3)
    assert not (await call("/state"))["signals"]["boiler.pressure_bar_abs"]["forced"]


async def test_events_and_metrics_endpoints(plant):
    events = await call("/events?limit=50")
    assert isinstance(events, list) and events
    assert any(e.get("event") == "PARTICIPANT_JOINED" for e in events)
    health = await call("/health")
    assert health["status"] == "ok"
