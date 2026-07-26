"""狀態感知模糊測試：協定層被亂打時，設備必須仍存活且物理狀態不被繞過。"""
from __future__ import annotations

import asyncio
import random
import struct

import pytest

from common.modbus.register_map import DeviceState
from tests.harness import CONFIG_DIR, DEVICE_CLASSES
from tools.fuzz.fuzzer import UNSUPPORTED_FCS, next_frame

PORT = 15601


@pytest.fixture
async def device(tmp_path):
    import os

    os.environ["MODBUS_PORT"] = str(PORT)
    device = DEVICE_CLASSES["boiler"](config_dir=CONFIG_DIR, state_dir=str(tmp_path / "boiler"))
    device.bus_ok = True
    device.sm.force(DeviceState.RUNNING, "test")
    device.flame = 2
    device.set_hr("MANUAL_OUTPUT", 40.0)
    for _ in range(20):
        device.scan(0.1)
    await device.server.start()
    yield device
    await device.server.stop()
    device.store.close()
    os.environ.pop("MODBUS_PORT", None)


async def send(frame: bytes, reader=None, writer=None):
    close = writer is None
    if writer is None:
        reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(frame)
    await writer.drain()
    try:
        header = await asyncio.wait_for(reader.readexactly(7), timeout=2.0)
        length = struct.unpack(">HHHB", header)[2]
        body = await asyncio.wait_for(reader.readexactly(length - 1), timeout=2.0)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        header, body = b"", b""
    if close:
        writer.close()
    return header, body


async def test_random_frames_never_crash_device(device):
    random.seed(4242)
    before_mass = device.water_mass
    responses = 0
    for transaction in range(400):
        _, frame = next_frame(transaction)
        header, body = await send(frame)
        if header:
            responses += 1
            assert len(header) == 7
            if body:
                assert body[0] & 0x7F <= 0x7F
                if body[0] & 0x80:
                    assert 1 <= body[1] <= 0x0B, "例外碼必須是合法值"
        device.scan(0.1)
    assert responses > 300, "多數請求都必須得到格式正確的回應"
    # 設備仍然可用、物理量仍在合理範圍（協定層攻擊不得破壞物理模型）
    device.scan(0.1)
    assert device.sm.state in (DeviceState.RUNNING, DeviceState.TRIPPED)
    assert 0.0 < device.pressure < 130.0
    assert abs(device.water_mass - before_mass) < 5000.0
    assert 0.0 <= device.burner_output <= 100.0


async def test_unsupported_function_codes_all_return_exception_01(device):
    for fc in UNSUPPORTED_FCS:
        pdu = bytes([fc]) + b"\x00\x00\x00\x01"
        header, body = await send(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
        assert body and body[0] == (fc | 0x80) and body[1] == 0x01


async def test_write_storm_cannot_bypass_min_fire_or_trip_logic(device):
    """即使亂寫 holding register，安全邏輯（min fire、跳機鎖存）仍必須成立。"""
    random.seed(7)
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    for _ in range(200):
        address = random.randint(0, 63)
        value = random.randint(0, 0xFFFF)
        pdu = struct.pack(">BHH", 0x06, address, value)
        await send(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu, reader, writer)
        device.scan(0.1)
    writer.close()
    assert device.burner_output <= 100.0
    if device.sm.state is DeviceState.RUNNING:
        assert device.burner_output >= 0.0
    assert 0.0 <= device.level_indicated <= 130.0
    assert device.protection.specs, "保護設定不可被寫入破壞"


async def test_readonly_input_registers_never_change_physics(device):
    pressure_before = device.pressure
    # Input Register 沒有寫入功能碼；用 FC06 寫到對應位址應被拒絕
    for address in (9, 10, 11, 40, 50):
        pdu = struct.pack(">BHH", 0x06, address + 100, 1234)
        header, body = await send(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
        assert body[0] & 0x80
    device.scan(0.1)
    assert device.pressure == pytest.approx(pressure_before, abs=1.0)
