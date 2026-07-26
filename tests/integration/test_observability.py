"""historian（observer 角色）與 HMI（代理）煙霧測試。"""
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

BUS_PORT = 17300
BUS_HTTP = 18300
HISTORIAN_HTTP = 18301
HMI_HTTP = 18302


async def get(url: str) -> tuple[int, str]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(url) as response:
            return response.status, await response.text()


@pytest.fixture
async def stack(tmp_path):
    os.environ.update({
        "SIM_BUS_HOST": "127.0.0.1", "SIM_BUS_PORT": str(BUS_PORT), "MODBUS_PORT": "0",
        "STATE_DIR": str(tmp_path / "historian"), "HTTP_PORT": str(HISTORIAN_HTTP),
        "SAMPLE_PERIOD": "0.2",
    })
    cfg = {"simulation": {"dt": 0.1, "speed": 30.0, "tick_timeout": 1.0,
                          "bus_port": BUS_PORT, "devices": ["boiler"]},
           "bus": {"http_port": BUS_HTTP}}
    bus = PlantBus(cfg, SnapshotStore(str(tmp_path / "snap")), EventLogger(device="plant-bus"))
    await bus.start()
    bus_runner = await start_http(bus, port=BUS_HTTP)

    from historian.main import Historian
    from hmi.main import build_app
    import hmi.main as hmi_main
    from aiohttp import web

    historian = Historian()
    historian_task = asyncio.ensure_future(historian.run())

    hmi_main.BUS_API = f"http://127.0.0.1:{BUS_HTTP}"
    hmi_runner = web.AppRunner(build_app(), access_log=None)
    await hmi_runner.setup()
    await web.TCPSite(hmi_runner, "127.0.0.1", HMI_HTTP).start()

    device = DEVICE_CLASSES["boiler"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "boiler"))
    device_task = asyncio.ensure_future(device.run())
    for _ in range(100):
        await asyncio.sleep(0.05)
        if historian.sample_count > 0 and bus.tick > 10:
            break

    yield bus, historian, device

    await device.shutdown()
    device_task.cancel()
    historian_task.cancel()
    await asyncio.gather(device_task, historian_task, return_exceptions=True)
    await historian.bus.close()
    await hmi_runner.cleanup()
    await bus.stop()
    await bus_runner.cleanup()
    for key in ("SIM_BUS_HOST", "SIM_BUS_PORT", "MODBUS_PORT", "STATE_DIR", "HTTP_PORT",
                "SAMPLE_PERIOD"):
        os.environ.pop(key, None)


async def test_historian_records_samples_and_events(stack):
    bus, historian, device = stack
    assert historian.sample_count > 0
    status, body = await get(f"http://127.0.0.1:{HISTORIAN_HTTP}/health")
    assert status == 200 and json.loads(body)["status"] == "ok"

    status, body = await get(
        f"http://127.0.0.1:{HISTORIAN_HTTP}/query?signal=boiler.pressure_bar_abs&limit=10")
    assert status == 200
    assert json.loads(body)["samples"], "應有歷史取樣"

    device._emit("TRIP_LATCHED", code=5101, name="LOW_LOW_LEVEL", first_out=True, value=18.0)
    await asyncio.sleep(0.5)
    status, body = await get(f"http://127.0.0.1:{HISTORIAN_HTTP}/first-out")
    assert status == 200
    status, body = await get(f"http://127.0.0.1:{HISTORIAN_HTTP}/events?limit=50")
    events = json.loads(body)
    assert any(e.get("event") == "TRIP_LATCHED" for e in events), "事件必須被記錄"

    status, body = await get(f"http://127.0.0.1:{HISTORIAN_HTTP}/metrics")
    assert status == 200 and "historian_samples_total" in body


async def test_hmi_serves_page_and_proxies_bus_api(stack):
    status, body = await get(f"http://127.0.0.1:{HMI_HTTP}/")
    assert status == 200 and "火力發電廠" in body

    status, body = await get(f"http://127.0.0.1:{HMI_HTTP}/api/state")
    assert status == 200
    state = json.loads(body)
    assert state["tick"] > 0 and "boiler.pressure_bar_abs" in state["signals"]

    status, body = await get(f"http://127.0.0.1:{HMI_HTTP}/api/snapshot")
    assert status == 200 and "snapshots" in json.loads(body)
