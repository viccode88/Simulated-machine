"""Modbus 例外碼與內部錯誤型別。"""
from __future__ import annotations

from enum import IntEnum


class ModbusException(IntEnum):
    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03
    SERVER_DEVICE_FAILURE = 0x04
    ACKNOWLEDGE = 0x05
    SERVER_DEVICE_BUSY = 0x06
    NEGATIVE_ACKNOWLEDGE = 0x07
    MEMORY_PARITY_ERROR = 0x08
    GATEWAY_PATH_UNAVAILABLE = 0x0A
    GATEWAY_TARGET_NO_RESPONSE = 0x0B


class ModbusError(Exception):
    """帶有 Modbus 例外碼的內部錯誤。"""

    def __init__(self, code: ModbusException, message: str = "") -> None:
        super().__init__(message or code.name)
        self.code = code
        self.message = message or code.name


EXCEPTION_MEANING = {
    ModbusException.ILLEGAL_FUNCTION: "功能碼不允許",
    ModbusException.ILLEGAL_DATA_ADDRESS: "地址不存在或區域不可存取",
    ModbusException.ILLEGAL_DATA_VALUE: "數值、列舉或長度錯誤",
    ModbusException.SERVER_DEVICE_FAILURE: "設備內部錯誤",
    ModbusException.SERVER_DEVICE_BUSY: "設備正在切換狀態或資料鎖定",
}
