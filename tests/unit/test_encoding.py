"""縮放整數與 32 位元編碼。"""
from common.modbus.encoding import (bits_to_word, dec_f32, dec_i16, dec_u16, dec_u32, enc_f32,
                                    enc_i16, enc_u16, enc_u32, word_to_bits)


def test_u16_scaling_round_trip():
    assert enc_u16(66.7, 100) == 6670
    assert dec_u16(6670, 100) == 66.7
    assert enc_u16(0.08, 10000) == 800
    assert abs(dec_u16(800, 10000) - 0.08) < 1e-9


def test_u16_clamps_instead_of_wrapping():
    assert enc_u16(-5.0, 100) == 0
    assert enc_u16(1000.0, 100) == 0xFFFF


def test_i16_negative():
    raw = enc_i16(-12.5, 10)
    assert raw == 0xFFFF - 125 + 1
    assert dec_i16(raw, 10) == -12.5


def test_u32_high_word_first():
    high, low = enc_u32(123456)
    assert high == (123456 >> 16) and low == (123456 & 0xFFFF)
    assert dec_u32(high, low) == 123456


def test_f32_big_endian_high_word_first():
    high, low = enc_f32(1.5)
    assert abs(dec_f32(high, low) - 1.5) < 1e-6
    assert high == 0x3FC0 and low == 0x0000


def test_bitfield():
    assert bits_to_word({0: True, 3: True}) == 0b1001
    assert word_to_bits(0b1001)[:4] == [True, False, False, True]
