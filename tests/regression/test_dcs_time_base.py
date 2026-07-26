"""端對端：plant-bus + 設備 + 真正的 DCS，在加速模擬下驗證時間基準。

原始缺陷：DCS 的 PID 掃描、啟動順序與 watchdog 都依真實時間執行，
`speed 5` 時 watchdog 每 5 模擬秒才更新一次，超過設備 3 模擬秒的
watchdog_timeout，導致整廠出現假的通訊逾時（CONTROL_WATCHDOG_LOST）。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from common.util import EventLogger
from plant_bus.app.bus import PlantBus
from tests.harness import CONFIG_DIR, DEVICE_CLASSES

BUS_PORT = 17300
BASE_MODBUS_PORT = 15300
DEVICES = ["condenser", "condensate_pump", "feedwater_tank", "feedwater_pump", "boiler"]


@pytest.fixture
async def plant_with_dcs(tmp_path):
    """以 5 倍速啟動小型機組與真正的 DCS 控制器。"""
    from controller.dcs.main import DCS

    cfg = {
        "simulation": {"dt": 0.1, "speed": 5.0, "tick_timeout": 1.0,
                       "bus_port": BUS_PORT, "devices": DEVICES},
        "bus": {"http_port": 0},
    }
    bus = PlantBus(cfg, None, EventLogger(device="plant-bus"))
    await bus.start()

    os.environ["SIM_BUS_HOST"] = "127.0.0.1"
    os.environ["SIM_BUS_PORT"] = str(BUS_PORT)
    os.environ["STATE_DIR"] = str(tmp_path / "dcs")
    os.environ["AUTO_START"] = "false"          # 只驗證時間基準，不跑完整啟動序列

    devices, tasks, ports = {}, [], {}
    for index, name in enumerate(DEVICES):
        ports[name] = BASE_MODBUS_PORT + index
        os.environ["MODBUS_PORT"] = str(ports[name])
        device = DEVICE_CLASSES[name](config_dir=CONFIG_DIR, state_dir=str(tmp_path / name))
        devices[name] = device
        tasks.append(asyncio.ensure_future(device.run()))
    os.environ.pop("MODBUS_PORT", None)

    # 等待各設備的 Modbus server 真的開始監聽，再建立 DCS 連線
    for _ in range(100):
        await asyncio.sleep(0.05)
        if all(d.server._server is not None for d in devices.values()):
            break

    dcs = DCS(config_dir=CONFIG_DIR)
    # 行程內測試：每台設備各佔一個 localhost 埠（容器版是各自的 host:502）
    for name, link in list(dcs.devices.items()):
        if name not in devices:
            dcs.devices.pop(name)
            continue
        link.host, link.port = "127.0.0.1", ports[name]
    dcs_task = asyncio.ensure_future(dcs.run())
    tasks.append(dcs_task)

    for _ in range(200):
        await asyncio.sleep(0.05)
        if len(bus.participants) >= len(DEVICES) + 1 and bus.tick > 10:
            break

    yield bus, devices, dcs

    await dcs.shutdown()
    for device in devices.values():
        await device.shutdown()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await bus.stop()
    dcs.log.close()
    for key in ("SIM_BUS_HOST", "SIM_BUS_PORT", "STATE_DIR", "AUTO_START"):
        os.environ.pop(key, None)


async def test_dcs_joins_bus_and_receives_simulation_time(plant_with_dcs):
    bus, _, dcs = plant_with_dcs
    assert "dcs-plc" in bus.participants, "DCS 必須以 CONTROLLER 身分加入"
    before = dcs.sim_time
    await asyncio.sleep(0.5)
    assert dcs.sim_time > before, "DCS 必須持續收到模擬時間（不依賴 observer 在線）"


async def test_no_false_watchdog_timeout_at_5x_speed(plant_with_dcs):
    """5 倍速下 watchdog 必須依模擬時間更新，不得觸發假的通訊逾時。"""
    _, devices, _ = plant_with_dcs
    for device in devices.values():
        device.alarms.states[device.CODE_BASE + 91].count = 0

    await asyncio.sleep(2.0)                     # 約 10 模擬秒，遠超過 3 秒門檻

    for name, device in devices.items():
        assert device.watchdog_ok, (
            f"{name} 的 watchdog 在 5 倍速下逾時：age={device.watchdog_age:.2f} 模擬秒，"
            f"門檻 {device.watchdog_timeout}"
        )
        assert device.watchdog_age < device.watchdog_timeout
        assert device.comm_loss_seconds == 0.0, f"{name} 出現通訊逾時累積"
        assert device.alarms.states[device.CODE_BASE + 91].count == 0, (
            f"{name} 觸發了 CONTROL_WATCHDOG_LOST 警報"
        )


async def test_watchdog_counter_advances_on_every_device(plant_with_dcs):
    _, devices, _ = plant_with_dcs
    before = {name: device.watchdog_value for name, device in devices.items()}
    await asyncio.sleep(1.0)
    for name, device in devices.items():
        assert device.watchdog_value != before[name], f"{name} 的 watchdog 計數器沒有前進"


async def test_devices_stay_paused_after_step(plant_with_dcs):
    """`pause; step N` 之後設備與 plant-bus 的暫停狀態必須一致。"""
    bus, devices, dcs = plant_with_dcs
    await bus.pause()
    await asyncio.sleep(0.3)
    assert bus.paused and all(d.bus_paused for d in devices.values())

    tick_before = bus.tick
    await bus.step(5)
    await asyncio.sleep(1.0)

    assert bus.tick == tick_before + 5, "只能前進指定的 tick 數"
    assert bus.paused, "步進結束後 plant-bus 必須回到暫停"
    for name, device in devices.items():
        assert device.bus_paused, f"{name} 仍以為模擬在執行（暫停狀態分裂）"
    assert dcs.paused, "DCS 也必須知道模擬已暫停"

    await bus.resume()
    await asyncio.sleep(0.3)
    assert not bus.paused and not any(d.bus_paused for d in devices.values())
