"""暫存器編碼工具。

規格要求：
* 主要程序量使用縮放整數，避免不同 PLC 對浮點 word order 的解讀差異。
* 32 位元值高 word 在前（High Word First）。
* Float32 若提供：Big Endian byte order + High Word First。
"""
from __future__ import annotations

import struct

U16_MAX = 0xFFFF
I16_MIN = -32768
I16_MAX = 32767


def enc_u16(value: float, scale: float = 1.0) -> int:
    """縮放後轉為 UInt16，超出範圍夾在邊界（品質碼另外標示 OUT_OF_RANGE）。"""
    raw = int(round(value * scale))
    if raw < 0:
        return 0
    if raw > U16_MAX:
        return U16_MAX
    return raw


def dec_u16(raw: int, scale: float = 1.0) -> float:
    return (raw & U16_MAX) / scale


def enc_i16(value: float, scale: float = 1.0) -> int:
    raw = int(round(value * scale))
    raw = max(I16_MIN, min(I16_MAX, raw))
    return raw & U16_MAX


def dec_i16(raw: int, scale: float = 1.0) -> float:
    raw &= U16_MAX
    if raw >= 0x8000:
        raw -= 0x10000
    return raw / scale


def enc_u32(value: float, scale: float = 1.0) -> tuple[int, int]:
    """回傳 (high_word, low_word)。"""
    raw = int(round(value * scale))
    raw = max(0, min(0xFFFFFFFF, raw))
    return (raw >> 16) & U16_MAX, raw & U16_MAX


def dec_u32(high: int, low: int, scale: float = 1.0) -> float:
    return (((high & U16_MAX) << 16) | (low & U16_MAX)) / scale


def enc_f32(value: float) -> tuple[int, int]:
    """Big Endian byte order、High Word First。"""
    raw = struct.pack(">f", float(value))
    high, low = struct.unpack(">HH", raw)
    return high, low


def dec_f32(high: int, low: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", high & U16_MAX, low & U16_MAX))[0]


def bits_to_word(bits: dict[int, bool] | list[bool]) -> int:
    """bit index -> bool 轉為 16 位元 bitfield。"""
    word = 0
    if isinstance(bits, dict):
        items = bits.items()
    else:
        items = enumerate(bits)
    for index, value in items:
        if value and 0 <= index < 16:
            word |= 1 << index
    return word & U16_MAX


def word_to_bits(word: int) -> list[bool]:
    return [bool(word & (1 << i)) for i in range(16)]
