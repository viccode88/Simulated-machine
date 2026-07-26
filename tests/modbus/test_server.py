"""Modbus TCP server 驗收：功能碼、例外碼、原子性、多 client。

以 pymodbus 3.14.0 當 client 驗證互通性。
"""
from __future__ import annotations

import asyncio
import struct

import pytest
from pymodbus.client import AsyncModbusTcpClient

from common.modbus.exceptions import ModbusException
from common.modbus.register_map import RegisterMap, RegSpec, Table
from common.modbus.server import ModbusTcpServer, RegisterImage, WriteRequest

PORT = 15599


class Fixture:
    def __init__(self) -> None:
        self.rmap = RegisterMap.build(
            "test",
            process_inputs=[
                RegSpec(9, "PV", "%", 100),
                RegSpec(39, "TOTAL_HI", "kg", 1, dtype="u32"),
                RegSpec(40, "TOTAL_LO", "kg", 1),
            ],
            extra_holdings=[
                RegSpec(9, "PRIMARY_SETPOINT", "%", 100, lo=0, hi=100, writable=True),
                RegSpec(11, "MANUAL_OUTPUT", "%", 100, lo=0, hi=100, writable=True),
            ],
        )
        self.writes: list[WriteRequest] = []
        self.image = RegisterImage.empty(self.rmap)
        self.busy = False
        self.server = ModbusTcpServer(
            self.rmap,
            image_provider=lambda: self.image,
            write_handler=self._write,
            port=PORT,
            busy_provider=lambda: self.busy,
            lab_mode=True,
        )

    def _write(self, request: WriteRequest):
        self.writes.append(request)
        holdings = list(self.image.holdings)
        coils = list(self.image.coils)
        if request.table is Table.HOLDING:
            for index, value in enumerate(request.values):
                holdings[request.offset + index] = value
        else:
            for index, value in enumerate(request.values):
                coils[request.offset + index] = bool(value)
        self.image = RegisterImage(coils=tuple(coils), discretes=self.image.discretes,
                                   inputs=self.image.inputs, holdings=tuple(holdings))
        return None

    def set_total(self, value: int) -> None:
        inputs = list(self.image.inputs)
        inputs[39] = (value >> 16) & 0xFFFF
        inputs[40] = value & 0xFFFF
        inputs[9] = 6670
        self.image = RegisterImage(coils=self.image.coils, discretes=self.image.discretes,
                                   inputs=tuple(inputs), holdings=self.image.holdings)


@pytest.fixture
async def server():
    fixture = Fixture()
    fixture.set_total(0x0001_0001)
    await fixture.server.start()
    yield fixture
    await fixture.server.stop()


@pytest.fixture
async def client(server):
    client = AsyncModbusTcpClient("127.0.0.1", port=PORT, timeout=3)
    await client.connect()
    yield client
    client.close()


async def raw(pdu: bytes, unit: int = 1, transaction: int = 7) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu)
    await writer.drain()
    header = await reader.readexactly(7)
    length = struct.unpack(">HHHB", header)[2]
    body = await reader.readexactly(length - 1)
    writer.close()
    return header + body


# --------------------------------------------------------------------- 讀取
async def test_read_input_registers(client, server):
    result = await client.read_input_registers(9, count=1, device_id=1)
    assert not result.isError()
    assert result.registers[0] == 6670


async def test_read_100_input_register_scan_block(client, server):
    """PLC I/O scanners may poll offset 0..99 even when only one tag is mapped."""
    inputs = list(server.image.inputs)
    inputs[19] = 0x1234
    server.image = RegisterImage(
        coils=server.image.coils,
        discretes=server.image.discretes,
        inputs=tuple(inputs),
        holdings=server.image.holdings,
    )

    result = await client.read_input_registers(0, count=100, device_id=1)

    assert not result.isError()
    assert len(result.registers) == 100
    assert result.registers[19] == 0x1234
    assert result.registers[99] == 0


async def test_read_holding_and_discrete_and_coils(client):
    assert not (await client.read_holding_registers(0, count=10, device_id=1)).isError()
    assert not (await client.read_discrete_inputs(0, count=16, device_id=1)).isError()
    assert not (await client.read_coils(0, count=8, device_id=1)).isError()


async def test_transaction_id_is_echoed(server):
    frame = await raw(struct.pack(">BHH", 0x04, 9, 1), transaction=0xBEEF)
    assert struct.unpack(">H", frame[0:2])[0] == 0xBEEF


# ------------------------------------------------------------------- 例外碼
async def test_unsupported_function_returns_exception_01_and_keeps_connection(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    for fc in (0x07, 0x14, 0x64):
        pdu = bytes([fc, 0x00])
        writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
        await writer.drain()
        header = await reader.readexactly(7)
        body = await reader.readexactly(struct.unpack(">HHHB", header)[2] - 1)
        assert body[0] == (fc | 0x80)
        assert body[1] == int(ModbusException.ILLEGAL_FUNCTION)
    # 連線必須仍可用
    pdu = struct.pack(">BHH", 0x04, 9, 1)
    writer.write(struct.pack(">HHHB", 2, 0, len(pdu) + 1, 1) + pdu)
    await writer.drain()
    header = await reader.readexactly(7)
    body = await reader.readexactly(struct.unpack(">HHHB", header)[2] - 1)
    assert body[0] == 0x04
    writer.close()


async def test_missing_address_returns_exception_02(client):
    result = await client.read_input_registers(1000, count=1, device_id=1)
    assert result.isError()
    assert result.exception_code == int(ModbusException.ILLEGAL_DATA_ADDRESS)


async def test_out_of_range_value_returns_exception_03(client, server):
    before = len(server.writes)
    result = await client.write_register(9, 60000, device_id=1)  # 600% > hi=100
    assert result.isError()
    assert result.exception_code == int(ModbusException.ILLEGAL_DATA_VALUE)
    assert len(server.writes) == before, "被拒絕的寫入不可進入 command queue"


async def test_write_to_readonly_area_returns_exception_02(client, server):
    before = len(server.writes)
    result = await client.write_register(30, 1234, device_id=1)  # 未定義 -> 唯讀
    assert result.isError()
    assert result.exception_code == int(ModbusException.ILLEGAL_DATA_ADDRESS)
    assert len(server.writes) == before


async def test_busy_returns_exception_06(client, server):
    server.busy = True
    result = await client.write_register(9, 5000, device_id=1)
    server.busy = False
    assert result.isError()
    assert result.exception_code == int(ModbusException.SERVER_DEVICE_BUSY)


async def test_illegal_count_returns_exception_03(server):
    frame = await raw(struct.pack(">BHH", 0x04, 0, 200))
    assert frame[7] == 0x84 and frame[8] == int(ModbusException.ILLEGAL_DATA_VALUE)


async def test_wrong_unit_id_returns_gateway_exception(server):
    frame = await raw(struct.pack(">BHH", 0x04, 9, 1), unit=9)
    assert frame[8] == int(ModbusException.GATEWAY_TARGET_NO_RESPONSE)


# --------------------------------------------------------------------- 寫入
async def test_write_single_and_multiple_registers(client, server):
    assert not (await client.write_register(9, 5000, device_id=1)).isError()
    assert not (await client.write_registers(9, [1000, 2000, 0, 3000], device_id=1)).isError()
    assert server.writes[-1].values == [1000, 2000, 0, 3000]


async def test_multi_register_write_is_atomic(client, server):
    before = len(server.writes)
    # 第二個值超出範圍 -> 整批拒絕
    result = await client.write_registers(9, [1000, 0, 0, 60000], device_id=1)
    assert result.isError()
    assert result.exception_code == int(ModbusException.ILLEGAL_DATA_VALUE)
    assert len(server.writes) == before


async def test_write_coils_and_single_coil(client, server):
    assert not (await client.write_coil(0, True, device_id=1)).isError()
    assert not (await client.write_coils(0, [True, False, True], device_id=1)).isError()


async def test_write_single_coil_rejects_invalid_value(server):
    frame = await raw(struct.pack(">BHH", 0x05, 0, 0x1234))
    assert frame[7] == 0x85 and frame[8] == int(ModbusException.ILLEGAL_DATA_VALUE)


async def test_mask_write_register(client, server):
    await client.write_register(9, 0x0F00, device_id=1)
    result = await client.mask_write_register(address=9, and_mask=0xFF00, or_mask=0x0012,
                                              device_id=1)
    assert not result.isError()
    assert server.writes[-1].values == [0x0F12]


async def test_read_write_multiple_registers(client, server):
    result = await client.readwrite_registers(read_address=0, read_count=4, write_address=9,
                                              values=[1234], device_id=1)
    assert not result.isError()
    assert len(result.registers) == 4
    assert server.writes[-1].values == [1234]


async def test_read_device_identification(client):
    result = await client.read_device_information(read_code=1, device_id=1)
    assert not result.isError()
    assert result.information[0]


# ----------------------------------------------------------------- 原子性
async def test_no_torn_read_of_32bit_value(server):
    """暫存器映像整份替換，因此 32 位元值不會出現高低 word 不一致。"""
    stop = asyncio.Event()

    async def writer():
        counter = 0
        while not stop.is_set():
            counter = (counter + 1) & 0xFFFF
            server.set_total((counter << 16) | counter)   # 高低 word 必須永遠相同
            await asyncio.sleep(0)

    task = asyncio.ensure_future(writer())
    client = AsyncModbusTcpClient("127.0.0.1", port=PORT, timeout=3)
    await client.connect()
    try:
        for _ in range(200):
            result = await client.read_input_registers(39, count=2, device_id=1)
            assert not result.isError()
            high, low = result.registers
            assert high == low, f"讀到撕裂值 {high:04x}{low:04x}"
    finally:
        stop.set()
        await task
        client.close()


async def test_at_least_eight_concurrent_readers(server):
    clients = [AsyncModbusTcpClient("127.0.0.1", port=PORT, timeout=3) for _ in range(10)]
    await asyncio.gather(*[c.connect() for c in clients])
    try:
        results = await asyncio.gather(
            *[c.read_input_registers(9, count=1, device_id=1) for c in clients]
        )
        assert all(not r.isError() and r.registers[0] == 6670 for r in results)
    finally:
        for c in clients:
            c.close()


async def test_single_writer_lease_blocks_other_controller(client, server):
    server.server.access.active_controller_ip = "10.0.0.9"
    server.server.access.lease_expiry = float("inf")
    result = await client.write_register(9, 1000, device_id=1)
    assert result.isError()
    assert result.exception_code == int(ModbusException.SERVER_DEVICE_BUSY)
    # 讀取仍必須可用
    assert not (await client.read_input_registers(9, count=1, device_id=1)).isError()


async def test_malformed_frames_do_not_crash_server(server):
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    writer.write(b"\x00\x01\x00\x00\x00\x99\x01\x03\x00")   # 長度不符
    await writer.drain()
    writer.close()
    # 伺服器仍可服務新連線
    frame = await raw(struct.pack(">BHH", 0x04, 9, 1))
    assert frame[7] == 0x04
