"""Modbus TCP Server（asyncio 實作）。

為什麼不直接用 pymodbus 當 server：
    驗收標準要求對「唯讀區寫入」「超出範圍的設定值」「設備忙碌」「未啟用功能碼」
    分別回傳 02 / 03 / 06 / 01，且不可關閉連線；同時需要注入協定層故障
    （延遲、丟棄、錯誤例外碼、連線重置）與逐筆請求稽核。
    自行實作 MBAP/PDU 可完整控制這些行為；pymodbus 3.14.0 仍固定作為
    client（DCS、測試、互通性驗證）使用，見 pyproject.toml。

MBAP Header: TransactionId(2) ProtocolId(2) Length(2) UnitId(1)
"""
from __future__ import annotations

import asyncio
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .exceptions import ModbusException
from .register_map import RegisterMap, Table

MAX_READ_BITS = 2000
MAX_READ_REGS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGS = 123

# 可由 safety_allowlist 來源直接寫入的線圈（不受單一寫入者租約限制）
SAFETY_COILS = ("EMERGENCY_STOP", "FORCE_SAFE")


@dataclass(frozen=True)
class RegisterImage:
    """原子暫存器映像：一次 scan 產生一份，讀取端只取參考，不會讀到半更新資料。"""

    coils: tuple[bool, ...]
    discretes: tuple[bool, ...]
    inputs: tuple[int, ...]
    holdings: tuple[int, ...]
    generation: int = 0

    @staticmethod
    def empty(rmap: RegisterMap) -> "RegisterImage":
        return RegisterImage(
            coils=tuple([False] * rmap.coil_size),
            discretes=tuple([False] * rmap.discrete_size),
            inputs=tuple([0] * rmap.input_size),
            holdings=tuple([0] * rmap.holding_size),
        )


@dataclass
class WriteRequest:
    table: Table
    offset: int
    values: list[int]
    client_ip: str
    unit_id: int
    function_code: int


@dataclass
class CommFaults:
    """協定層故障注入（僅 LAB_MODE 開放）。與物理層故障分開，便於分辨問題來源。"""

    response_delay_ms: float = 0.0
    drop_request_prob: float = 0.0
    drop_response_prob: float = 0.0
    force_busy_prob: float = 0.0
    wrong_exception_prob: float = 0.0
    connection_reset_prob: float = 0.0
    freeze: bool = False  # 完全不回應，模擬設備通訊卡死
    rate_limit_per_s: float = 0.0  # 0 = 不限制

    def reset(self) -> None:
        self.response_delay_ms = 0.0
        self.drop_request_prob = 0.0
        self.drop_response_prob = 0.0
        self.force_busy_prob = 0.0
        self.wrong_exception_prob = 0.0
        self.connection_reset_prob = 0.0
        self.freeze = False
        self.rate_limit_per_s = 0.0

    def active_word(self) -> int:
        bits = [
            self.response_delay_ms > 0,
            self.drop_request_prob > 0,
            self.drop_response_prob > 0,
            self.force_busy_prob > 0,
            self.wrong_exception_prob > 0,
            self.connection_reset_prob > 0,
            self.freeze,
            self.rate_limit_per_s > 0,
        ]
        return sum(1 << i for i, b in enumerate(bits) if b)


@dataclass
class AccessPolicy:
    """寫入控制權：同一時間只允許一個有效寫入控制器。"""

    write_allowlist: list[str] = field(default_factory=list)   # 空 = 允許全部
    safety_allowlist: list[str] = field(default_factory=list)  # 緊急停止專用來源
    lease_seconds: float = 5.0
    enforce_single_writer: bool = True
    active_controller_ip: str | None = None
    active_controller_id: int = 0
    lease_expiry: float = 0.0

    def _allowed(self, ip: str) -> bool:
        return not self.write_allowlist or ip in self.write_allowlist

    def check_write(self, ip: str, now: float, is_safety: bool = False) -> ModbusException | None:
        if is_safety and self.safety_allowlist and ip in self.safety_allowlist:
            return None
        if not self._allowed(ip):
            return ModbusException.ILLEGAL_DATA_ADDRESS
        if not self.enforce_single_writer:
            return None
        if self.active_controller_ip is None or now >= self.lease_expiry:
            self.active_controller_ip = ip
            self.lease_expiry = now + self.lease_seconds
            return None
        if self.active_controller_ip == ip:
            self.lease_expiry = now + self.lease_seconds
            return None
        return ModbusException.SERVER_DEVICE_BUSY

    def snapshot(self) -> dict:
        return {
            "active_controller_ip": self.active_controller_ip,
            "active_controller_id": self.active_controller_id,
            "lease_remaining": max(0.0, self.lease_expiry - time.monotonic()),
        }


@dataclass
class DeviceIdentification:
    vendor_name: str = "OpenPlantSim"
    product_code: str = "TPS-DEV"
    revision: str = "3.0.0"
    vendor_url: str = "https://example.invalid/thermal-plant-simulator"
    product_name: str = "Thermal Plant Device Simulator"
    model_name: str = "GENERIC"
    app_name: str = "device"

    def objects(self) -> dict[int, bytes]:
        return {
            0x00: self.vendor_name.encode(),
            0x01: self.product_code.encode(),
            0x02: self.revision.encode(),
            0x03: self.vendor_url.encode(),
            0x04: self.product_name.encode(),
            0x05: self.model_name.encode(),
            0x06: self.app_name.encode(),
        }


class ModbusTcpServer:
    def __init__(
        self,
        rmap: RegisterMap,
        image_provider: Callable[[], RegisterImage],
        write_handler: Callable[[WriteRequest], ModbusException | None],
        *,
        unit_id: int = 1,
        host: str = "0.0.0.0",
        port: int = 502,
        max_clients: int = 32,
        busy_provider: Callable[[], bool] | None = None,
        on_request: Callable[[dict], None] | None = None,
        identification: DeviceIdentification | None = None,
        access: AccessPolicy | None = None,
        faults: CommFaults | None = None,
        lab_mode: bool = False,
    ) -> None:
        self.rmap = rmap
        self.image_provider = image_provider
        self.write_handler = write_handler
        self.unit_id = unit_id
        self.host = host
        self.port = port
        self.max_clients = max_clients
        self.busy_provider = busy_provider or (lambda: False)
        self.on_request = on_request
        self.ident = identification or DeviceIdentification(model_name=rmap.device.upper())
        self.access = access or AccessPolicy()
        self.faults = faults or CommFaults()
        self.lab_mode = lab_mode
        self.request_count = 0
        self.exception_count = 0
        self.client_count = 0
        self._server: asyncio.AbstractServer | None = None
        self._rate_tokens = 0.0
        self._rate_last = time.monotonic()

    # -- 生命週期 ----------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- 連線處理 ----------------------------------------------------------
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = peer[0]
        if self.client_count >= self.max_clients:
            writer.close()
            return
        self.client_count += 1
        try:
            while True:
                header = await reader.readexactly(7)
                txid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0:
                    # 非 Modbus 協定 -> 丟棄整個連線，但不可讓行程崩潰
                    break
                remaining = length - 1
                if remaining < 1 or remaining > 253:
                    break
                pdu = await reader.readexactly(remaining)
                await self._process(txid, unit, pdu, ip, writer)
                if writer.is_closing():
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防禦性
            if self.on_request:
                self.on_request({"client_ip": ip, "event": "HANDLER_ERROR", "error": repr(exc)})
        finally:
            self.client_count -= 1
            try:
                writer.close()
            except Exception:
                pass

    def _rate_limited(self) -> bool:
        if self.faults.rate_limit_per_s <= 0:
            return False
        now = time.monotonic()
        self._rate_tokens = min(
            self.faults.rate_limit_per_s,
            self._rate_tokens + (now - self._rate_last) * self.faults.rate_limit_per_s,
        )
        self._rate_last = now
        if self._rate_tokens < 1.0:
            return True
        self._rate_tokens -= 1.0
        return False

    async def _process(
        self, txid: int, unit: int, pdu: bytes, ip: str, writer: asyncio.StreamWriter
    ) -> None:
        start = time.perf_counter()
        self.request_count += 1
        fc = pdu[0]
        record: dict = {
            "client_ip": ip,
            "transaction_id": txid,
            "unit_id": unit,
            "function_code": fc,
            "raw": pdu.hex(),
        }

        if self.faults.freeze:
            record.update(result="FROZEN")
            self._log(record, start)
            return
        if self.lab_mode and self.faults.drop_request_prob and random.random() < self.faults.drop_request_prob:
            record.update(result="REQUEST_DROPPED")
            self._log(record, start)
            return
        if self.lab_mode and self.faults.connection_reset_prob and random.random() < self.faults.connection_reset_prob:
            record.update(result="CONNECTION_RESET")
            self._log(record, start)
            writer.transport.abort()  # type: ignore[union-attr]
            return

        if unit not in (self.unit_id, 0, 0xFF):
            response = self._exception(fc, ModbusException.GATEWAY_TARGET_NO_RESPONSE)
            record.update(result="WRONG_UNIT", exception_code=int(ModbusException.GATEWAY_TARGET_NO_RESPONSE))
        elif self._rate_limited():
            response = self._exception(fc, ModbusException.SERVER_DEVICE_BUSY)
            record.update(result="RATE_LIMITED", exception_code=int(ModbusException.SERVER_DEVICE_BUSY))
        elif self.lab_mode and self.faults.force_busy_prob and random.random() < self.faults.force_busy_prob:
            response = self._exception(fc, ModbusException.SERVER_DEVICE_BUSY)
            record.update(result="FORCED_BUSY", exception_code=int(ModbusException.SERVER_DEVICE_BUSY))
        elif self.lab_mode and self.faults.wrong_exception_prob and random.random() < self.faults.wrong_exception_prob:
            bogus = random.choice([0x01, 0x02, 0x03, 0x04, 0x06, 0x0A])
            response = self._exception(fc, ModbusException(bogus))
            record.update(result="INJECTED_EXCEPTION", exception_code=bogus)
        else:
            try:
                response, extra = self._dispatch(fc, pdu, ip)
                record.update(extra)
                if len(response) >= 2 and response[0] & 0x80:
                    record.update(result="EXCEPTION", exception_code=response[1])
                else:
                    record.setdefault("result", "OK")
            except Exception as exc:  # pragma: no cover - 防禦性
                response = self._exception(fc, ModbusException.SERVER_DEVICE_FAILURE)
                record.update(result="INTERNAL_ERROR", error=repr(exc),
                              exception_code=int(ModbusException.SERVER_DEVICE_FAILURE))

        if record.get("exception_code"):
            self.exception_count += 1

        if self.faults.response_delay_ms > 0:
            await asyncio.sleep(self.faults.response_delay_ms / 1000.0)
        if self.lab_mode and self.faults.drop_response_prob and random.random() < self.faults.drop_response_prob:
            record.update(result="RESPONSE_DROPPED")
            self._log(record, start)
            return

        frame = struct.pack(">HHHB", txid, 0, len(response) + 1, unit) + response
        writer.write(frame)
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        self._log(record, start)

    def _log(self, record: dict, start: float) -> None:
        record["latency_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        if self.on_request:
            self.on_request(record)

    # -- PDU 分派 ----------------------------------------------------------
    @staticmethod
    def _exception(fc: int, code: ModbusException) -> bytes:
        return struct.pack(">BB", (fc & 0x7F) | 0x80, int(code))

    def _dispatch(self, fc: int, pdu: bytes, ip: str) -> tuple[bytes, dict]:
        body = pdu[1:]
        if fc in (0x01, 0x02):
            return self._read_bits(fc, body)
        if fc in (0x03, 0x04):
            return self._read_regs(fc, body)
        if fc == 0x05:
            return self._write_single_coil(body, ip)
        if fc == 0x06:
            return self._write_single_register(body, ip)
        if fc == 0x0F:
            return self._write_multiple_coils(body, ip)
        if fc == 0x10:
            return self._write_multiple_registers(body, ip)
        if fc == 0x16:
            return self._mask_write_register(body, ip)
        if fc == 0x17:
            return self._read_write_registers(body, ip)
        if fc == 0x2B:
            return self._device_identification(body)
        # 未啟用功能碼：回傳 Exception 01，不可關閉連線
        return self._exception(fc, ModbusException.ILLEGAL_FUNCTION), {"result": "ILLEGAL_FUNCTION"}

    # -- 讀取 --------------------------------------------------------------
    def _read_bits(self, fc: int, body: bytes) -> tuple[bytes, dict]:
        if len(body) != 4:
            return self._exception(fc, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, count = struct.unpack(">HH", body)
        info = {"address": address, "count": count}
        if count < 1 or count > MAX_READ_BITS:
            return self._exception(fc, ModbusException.ILLEGAL_DATA_VALUE), info
        table = Table.COIL if fc == 0x01 else Table.DISCRETE
        image = self.image_provider()
        data = image.coils if table is Table.COIL else image.discretes
        if address + count > len(data):
            return self._exception(fc, ModbusException.ILLEGAL_DATA_ADDRESS), info
        bits = data[address : address + count]
        byte_count = (count + 7) // 8
        payload = bytearray(byte_count)
        for index, value in enumerate(bits):
            if value:
                payload[index // 8] |= 1 << (index % 8)
        return struct.pack(">BB", fc, byte_count) + bytes(payload), info

    def _read_regs(self, fc: int, body: bytes) -> tuple[bytes, dict]:
        if len(body) != 4:
            return self._exception(fc, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, count = struct.unpack(">HH", body)
        info = {"address": address, "count": count}
        if count < 1 or count > MAX_READ_REGS:
            return self._exception(fc, ModbusException.ILLEGAL_DATA_VALUE), info
        image = self.image_provider()
        data = image.holdings if fc == 0x03 else image.inputs
        if address + count > len(data):
            return self._exception(fc, ModbusException.ILLEGAL_DATA_ADDRESS), info
        regs = data[address : address + count]
        return struct.pack(">BB", fc, count * 2) + struct.pack(f">{count}H", *regs), info

    # -- 寫入 --------------------------------------------------------------
    def _guard_write(self, fc: int, table: Table, offset: int, count: int, ip: str,
                     is_safety: bool) -> ModbusException | None:
        specs = self.rmap.table(table)
        if offset + count > self.rmap.size(table):
            return ModbusException.ILLEGAL_DATA_ADDRESS
        for addr in range(offset, offset + count):
            spec = specs.get(addr)
            if spec is None or not spec.writable:
                # 不存在或唯讀 -> 02（寫入唯讀區不可靜默接受）
                return ModbusException.ILLEGAL_DATA_ADDRESS
        if self.busy_provider():
            return ModbusException.SERVER_DEVICE_BUSY
        return self.access.check_write(ip, time.monotonic(), is_safety=is_safety)

    def _safety_coil_offsets(self) -> set[int]:
        offsets = set()
        for name in SAFETY_COILS:
            try:
                offsets.add(self.rmap.offset_of(Table.COIL, name))
            except KeyError:
                continue
        return offsets

    def _is_safety_write(self, table: Table, offset: int, count: int) -> bool:
        """整批位址「全部」都是安全線圈時才算 safety write。

        只要批次內混進 START／RESET_TRIP 等非安全線圈，就不能沿用安全來源的
        特權；否則具 E-STOP 權限的來源可以用 FC15 一次寫入整段線圈區，
        繞過 write_allowlist 與單一寫入者租約。
        """
        if table is not Table.COIL or count < 1:
            return False
        safety = self._safety_coil_offsets()
        if not safety:
            return False
        return all(addr in safety for addr in range(offset, offset + count))

    def _validate_values(self, table: Table, offset: int, values: list[int]) -> ModbusException | None:
        specs = self.rmap.table(table)
        for index, raw in enumerate(values):
            if not 0 <= raw <= 0xFFFF:
                return ModbusException.ILLEGAL_DATA_VALUE
            spec = specs[offset + index]
            if table is Table.HOLDING and spec.lo is not None and spec.hi is not None:
                if spec.dtype == "i16":
                    value = (raw - 0x10000 if raw >= 0x8000 else raw) / spec.scale
                else:
                    value = raw / spec.scale
                if value < spec.lo - 1e-9 or value > spec.hi + 1e-9:
                    return ModbusException.ILLEGAL_DATA_VALUE
        return None

    def _apply(self, table: Table, offset: int, values: list[int], ip: str, fc: int) -> ModbusException | None:
        return self.write_handler(
            WriteRequest(table=table, offset=offset, values=values, client_ip=ip,
                         unit_id=self.unit_id, function_code=fc)
        )

    def _write_single_coil(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) != 4:
            return self._exception(0x05, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, raw = struct.unpack(">HH", body)
        info = {"address": address, "count": 1, "values": [raw]}
        if raw not in (0x0000, 0xFF00):
            return self._exception(0x05, ModbusException.ILLEGAL_DATA_VALUE), info
        err = self._guard_write(0x05, Table.COIL, address, 1, ip, self._is_safety_write(Table.COIL, address, 1))
        if err:
            return self._exception(0x05, err), info
        err = self._apply(Table.COIL, address, [1 if raw == 0xFF00 else 0], ip, 0x05)
        if err:
            return self._exception(0x05, err), info
        return struct.pack(">BHH", 0x05, address, raw), info

    def _write_single_register(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) != 4:
            return self._exception(0x06, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, value = struct.unpack(">HH", body)
        info = {"address": address, "count": 1, "values": [value]}
        err = self._guard_write(0x06, Table.HOLDING, address, 1, ip, False)
        if err:
            return self._exception(0x06, err), info
        err = self._validate_values(Table.HOLDING, address, [value]) or self._apply(
            Table.HOLDING, address, [value], ip, 0x06
        )
        if err:
            return self._exception(0x06, err), info
        return struct.pack(">BHH", 0x06, address, value), info

    def _write_multiple_coils(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) < 5:
            return self._exception(0x0F, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, count, byte_count = struct.unpack(">HHB", body[:5])
        info = {"address": address, "count": count}
        if count < 1 or count > MAX_WRITE_BITS or byte_count != (count + 7) // 8 or len(body) != 5 + byte_count:
            return self._exception(0x0F, ModbusException.ILLEGAL_DATA_VALUE), info
        payload = body[5:]
        values = [1 if payload[i // 8] & (1 << (i % 8)) else 0 for i in range(count)]
        info["values"] = values
        err = self._guard_write(0x0F, Table.COIL, address, count, ip,
                                self._is_safety_write(Table.COIL, address, count))
        if err:
            return self._exception(0x0F, err), info
        err = self._apply(Table.COIL, address, values, ip, 0x0F)
        if err:
            return self._exception(0x0F, err), info
        return struct.pack(">BHH", 0x0F, address, count), info

    def _write_multiple_registers(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) < 5:
            return self._exception(0x10, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, count, byte_count = struct.unpack(">HHB", body[:5])
        info = {"address": address, "count": count}
        if count < 1 or count > MAX_WRITE_REGS or byte_count != count * 2 or len(body) != 5 + byte_count:
            return self._exception(0x10, ModbusException.ILLEGAL_DATA_VALUE), info
        values = list(struct.unpack(f">{count}H", body[5:]))
        info["values"] = values
        err = self._guard_write(0x10, Table.HOLDING, address, count, ip, False)
        if err:
            return self._exception(0x10, err), info
        # 原子性：整批驗證通過才交給 command queue，任何一個失敗則全部不套用
        err = self._validate_values(Table.HOLDING, address, values) or self._apply(
            Table.HOLDING, address, values, ip, 0x10
        )
        if err:
            return self._exception(0x10, err), info
        return struct.pack(">BHH", 0x10, address, count), info

    def _mask_write_register(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) != 6:
            return self._exception(0x16, ModbusException.ILLEGAL_DATA_VALUE), {}
        address, and_mask, or_mask = struct.unpack(">HHH", body)
        info = {"address": address, "count": 1, "and_mask": and_mask, "or_mask": or_mask}
        err = self._guard_write(0x16, Table.HOLDING, address, 1, ip, False)
        if err:
            return self._exception(0x16, err), info
        image = self.image_provider()
        current = image.holdings[address]
        value = (current & and_mask) | (or_mask & ~and_mask & 0xFFFF)
        info["values"] = [value]
        err = self._validate_values(Table.HOLDING, address, [value]) or self._apply(
            Table.HOLDING, address, [value], ip, 0x16
        )
        if err:
            return self._exception(0x16, err), info
        return struct.pack(">BHHH", 0x16, address, and_mask, or_mask), info

    def _read_write_registers(self, body: bytes, ip: str) -> tuple[bytes, dict]:
        if len(body) < 9:
            return self._exception(0x17, ModbusException.ILLEGAL_DATA_VALUE), {}
        read_addr, read_count, write_addr, write_count, byte_count = struct.unpack(">HHHHB", body[:9])
        info = {"address": write_addr, "count": write_count, "read_address": read_addr,
                "read_count": read_count}
        if (
            read_count < 1
            or read_count > MAX_READ_REGS
            or write_count < 1
            or write_count > MAX_WRITE_REGS
            or byte_count != write_count * 2
            or len(body) != 9 + byte_count
        ):
            return self._exception(0x17, ModbusException.ILLEGAL_DATA_VALUE), info
        values = list(struct.unpack(f">{write_count}H", body[9:]))
        info["values"] = values
        err = self._guard_write(0x17, Table.HOLDING, write_addr, write_count, ip, False)
        if err:
            return self._exception(0x17, err), info
        image = self.image_provider()
        if read_addr + read_count > len(image.holdings):
            return self._exception(0x17, ModbusException.ILLEGAL_DATA_ADDRESS), info
        err = self._validate_values(Table.HOLDING, write_addr, values) or self._apply(
            Table.HOLDING, write_addr, values, ip, 0x17
        )
        if err:
            return self._exception(0x17, err), info
        regs = image.holdings[read_addr : read_addr + read_count]
        return struct.pack(">BB", 0x17, read_count * 2) + struct.pack(f">{read_count}H", *regs), info

    # -- FC 43 / MEI 14 ----------------------------------------------------
    def _device_identification(self, body: bytes) -> tuple[bytes, dict]:
        if len(body) < 3 or body[0] != 0x0E:
            return self._exception(0x2B, ModbusException.ILLEGAL_FUNCTION), {}
        read_code, object_id = body[1], body[2]
        info = {"mei": 0x0E, "read_code": read_code, "object_id": object_id}
        if read_code not in (0x01, 0x02, 0x03, 0x04):
            return self._exception(0x2B, ModbusException.ILLEGAL_DATA_VALUE), info
        objects = self.ident.objects()
        if read_code == 0x01:
            selected = {k: v for k, v in objects.items() if k <= 0x02}
        elif read_code == 0x02:
            selected = {k: v for k, v in objects.items() if 0x03 <= k <= 0x06}
        elif read_code == 0x03:
            selected = objects
        else:
            if object_id not in objects:
                return self._exception(0x2B, ModbusException.ILLEGAL_DATA_ADDRESS), info
            selected = {object_id: objects[object_id]}
        payload = bytearray(struct.pack(">BBBBBB", 0x2B, 0x0E, read_code, 0x83, 0x00, 0x00))
        payload.append(len(selected))
        for oid in sorted(selected):
            value = selected[oid][:255]
            payload += struct.pack(">BB", oid, len(value)) + value
        return bytes(payload), info
