"""Modbus TCP frame 產生器：協定層 + 狀態感知模糊測試。"""
from __future__ import annotations

import random
import struct

VALID_FCS = [0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0F, 0x10, 0x16, 0x17, 0x2B]
UNSUPPORTED_FCS = [0x07, 0x08, 0x0B, 0x0C, 0x11, 0x14, 0x15, 0x18, 0x41, 0x64, 0x7F]


def mbap(transaction: int, pdu: bytes, unit: int = 1, length_override: int | None = None) -> bytes:
    length = len(pdu) + 1 if length_override is None else length_override
    return struct.pack(">HHHB", transaction & 0xFFFF, 0, length & 0xFFFF, unit & 0xFF) + pdu


def valid_read(transaction: int, unit: int = 1) -> bytes:
    fc = random.choice([0x01, 0x02, 0x03, 0x04])
    address = random.randint(0, 60)
    count = random.randint(1, 8)
    return mbap(transaction, struct.pack(">BHH", fc, address, count), unit)


def boundary_read(transaction: int, unit: int = 1) -> bytes:
    """邊界：不存在的地址、超量讀取。應回 Exception 02 / 03，不可斷線。"""
    fc = random.choice([0x01, 0x02, 0x03, 0x04])
    address = random.choice([0, 63, 64, 65535, 65534, 1000])
    count = random.choice([0, 1, 125, 126, 2000, 2001, 65535])
    return mbap(transaction, struct.pack(">BHH", fc, address, count), unit)


def out_of_range_write(transaction: int, unit: int = 1) -> bytes:
    """超出工程範圍的設定值，應回 Exception 03 且不得改變物理狀態。"""
    address = random.choice([0, 1, 6, 9, 10, 11, 12, 13, 24, 29])
    value = random.choice([0xFFFF, 0xFFFE, 0x8000, 60000, 40000])
    return mbap(transaction, struct.pack(">BHH", 0x06, address, value), unit)


def readonly_write(transaction: int, unit: int = 1) -> bytes:
    """對唯讀區寫入：Input Register 沒有寫入功能碼，改以寫到未定義 holding 位址驗證。"""
    address = random.choice([14, 15, 16, 17, 18, 25, 26, 27, 40, 50, 63])
    return mbap(transaction, struct.pack(">BHH", 0x06, address, random.randint(0, 0xFFFF)), unit)


def unsupported_function(transaction: int, unit: int = 1) -> bytes:
    fc = random.choice(UNSUPPORTED_FCS)
    payload = bytes(random.randint(0, 255) for _ in range(random.randint(0, 8)))
    return mbap(transaction, bytes([fc]) + payload, unit)


def malformed_length(transaction: int, unit: int = 1) -> bytes:
    pdu = struct.pack(">BHH", 0x03, 0, 2)
    bad_length = random.choice([0, 1, 2, 300, 65535])
    return mbap(transaction, pdu, unit, length_override=bad_length)


def truncated_pdu(transaction: int, unit: int = 1) -> bytes:
    pdu = struct.pack(">BHH", 0x10, 0, 4)[: random.randint(1, 4)]
    return mbap(transaction, pdu, unit)


def wrong_byte_count(transaction: int, unit: int = 1) -> bytes:
    address, count = random.randint(0, 20), random.randint(1, 8)
    declared = random.choice([0, 1, count * 2 + 1, 255])
    body = struct.pack(">BHHB", 0x10, address, count, declared)
    body += bytes(random.randint(0, 255) for _ in range(count * 2))
    return mbap(transaction, body, unit)


def wrong_unit(transaction: int) -> bytes:
    return mbap(transaction, struct.pack(">BHH", 0x03, 0, 1), random.choice([2, 3, 200, 254]))


def random_bytes(transaction: int, unit: int = 1) -> bytes:
    length = random.randint(1, 40)
    return mbap(transaction, bytes(random.randint(0, 255) for _ in range(length)), unit)


def device_identification(transaction: int, unit: int = 1) -> bytes:
    return mbap(transaction, struct.pack(">BBBB", 0x2B, 0x0E,
                                         random.choice([1, 2, 3, 4, 9]),
                                         random.choice([0, 1, 6, 200])), unit)


def stateful_write(transaction: int, unit: int, register_hint: tuple[int, int]) -> bytes:
    """狀態感知：針對真的可寫、且範圍已知的暫存器做邊界攻擊。"""
    address, scale_max = register_hint
    value = random.choice([0, 1, scale_max, scale_max + 1, scale_max * 2, 0xFFFF])
    return mbap(transaction, struct.pack(">BHH", 0x06, address, min(0xFFFF, value)), unit)


GENERATORS = [
    valid_read, boundary_read, out_of_range_write, readonly_write,
    unsupported_function, malformed_length, truncated_pdu, wrong_byte_count,
    random_bytes, device_identification,
]


def next_frame(transaction: int, unit: int = 1) -> tuple[str, bytes]:
    if random.random() < 0.05:
        return "wrong_unit", wrong_unit(transaction)
    generator = random.choice(GENERATORS)
    return generator.__name__, generator(transaction, unit)
