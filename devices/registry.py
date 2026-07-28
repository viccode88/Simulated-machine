"""設備登錄表：名稱 -> 設備類別，以及該設備的暫存器映射。

工具（文件產生器、plantctl、測試）都從這裡取得唯一的設備清單與順序，
不需要再經過任何控制器程式。
"""
from __future__ import annotations

from common.modbus.register_map import RegisterMap

from devices.boiler.main import Boiler
from devices.condensate_pump.main import CondensatePump
from devices.condenser.main import Condenser
from devices.feedwater_pump.main import FeedwaterPump
from devices.feedwater_tank.main import FeedwaterTank
from devices.generator.main import Generator
from devices.steam_valve.main import SteamValve
from devices.turbine.main import Turbine

# 順序即為全廠的製程順序（冷凝器 -> 發電機），文件與 OpenPLC 位址皆依此排列
DEVICE_CLASSES = {
    "condenser": Condenser,
    "condensate_pump": CondensatePump,
    "feedwater_tank": FeedwaterTank,
    "feedwater_pump": FeedwaterPump,
    "boiler": Boiler,
    "steam_valve": SteamValve,
    "turbine": Turbine,
    "generator": Generator,
}


def build_map(name: str) -> RegisterMap:
    klass = DEVICE_CLASSES[name]
    return RegisterMap.build(name, klass.PROCESS_INPUTS, klass.EXTRA_HOLDINGS, klass.EXTRA_COILS)
