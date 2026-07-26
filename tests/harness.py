"""測試用的行程內迷你機組：不需要 docker 也能跑完整 lockstep 物理迴圈。"""
from __future__ import annotations

import os
import tempfile

from common.modbus.register_map import Table
from common.simbus.protocol import SignalValue

from devices.boiler.main import Boiler
from devices.condensate_pump.main import CondensatePump
from devices.condenser.main import Condenser
from devices.feedwater_pump.main import FeedwaterPump
from devices.feedwater_tank.main import FeedwaterTank
from devices.generator.main import Generator
from devices.steam_valve.main import SteamValve
from devices.turbine.main import Turbine

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs")

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


class MiniPlant:
    """以 lockstep 方式驅動設備：所有設備都用『上一個 tick 已確認的鄰接輸出』計算。"""

    def __init__(self, names: list[str] | None = None, state_root: str | None = None,
                 lab_mode: bool = True, dt: float = 0.1) -> None:
        self.dt = dt
        self.tick = 0
        self.sim_time = 0.0
        self.state_root = state_root or tempfile.mkdtemp(prefix="miniplant-")
        os.environ["LAB_MODE"] = "true" if lab_mode else "false"
        self.devices = {}
        for name in (names or list(DEVICE_CLASSES)):
            path = os.path.join(self.state_root, name)
            os.makedirs(path, exist_ok=True)
            self.devices[name] = DEVICE_CLASSES[name](config_dir=CONFIG_DIR, state_dir=path)
        self.signals: dict[str, float] = {}
        for device in self.devices.values():
            device.bus_ok = True
            self.signals.update(device.publish())

    # -- 執行 --------------------------------------------------------------
    def step(self, ticks: int = 1, kick_watchdog: bool = True) -> None:
        for _ in range(ticks):
            self.tick += 1
            self.sim_time = round(self.sim_time + self.dt, 6)
            frozen = dict(self.signals)
            outputs: dict[str, float] = {}
            for device in self.devices.values():
                if kick_watchdog:
                    self.kick(device)
                device.tick = self.tick
                device.sim_time = self.sim_time
                device.bus_ok = True
                device.inputs = {
                    name: SignalValue(frozen.get(name, 0.0), "GOOD", self.tick, "test")
                    for name in device.SUBSCRIBES
                }
                device.scan(self.dt)
                outputs.update(device.publish())
            self.signals.update(outputs)

    def run_seconds(self, seconds: float, **kwargs) -> None:
        self.step(int(round(seconds / self.dt)), **kwargs)

    # -- 操作 --------------------------------------------------------------
    def kick(self, device) -> None:
        offset = device.rmap.offset_of(Table.HOLDING, "WATCHDOG_COUNTER")
        device.hold[offset] = (device.hold[offset] % 60000) + 1
        device.watchdog_value = device.hold[offset]
        device.watchdog_age = 0.0

    def dev(self, name: str):
        return self.devices[name]

    def write(self, name: str, register: str, value: float) -> None:
        self.devices[name].set_hr(register, value)

    def pulse(self, name: str, coil: str) -> None:
        device = self.devices[name]
        device.coil[device.rmap.offset_of(Table.COIL, coil)] = True

    def signal(self, name: str, default: float = 0.0) -> float:
        return self.signals.get(name, default)

    def inventory(self) -> float:
        return (
            self.signal("boiler.water_mass_kg")
            + self.signal("feedwater_tank.water_mass_kg")
            + self.signal("condenser.hotwell_mass_kg")
        )

    # -- 快照 --------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "bus": {"tick": self.tick, "sim_time": self.sim_time, "signals": dict(self.signals)},
            "participants": {name: device.snapshot_state()
                             for name, device in self.devices.items()},
        }

    def restore(self, document: dict, options: dict | None = None) -> None:
        bus = document["bus"]
        self.tick = bus["tick"]
        self.sim_time = bus["sim_time"]
        self.signals = dict(bus["signals"])
        for name, state in document["participants"].items():
            self.devices[name].restore_state(state, options or {})

    def close(self) -> None:
        for device in self.devices.values():
            device.store.close()
            device.log.close()


def bring_to_steady(plant: MiniPlant, load_mw: float = 60.0, seconds: float = 900.0) -> None:
    """以簡化流程把機組帶到接近穩態，供物理與快照測試使用。"""
    plant.write("condenser", "MANUAL_OUTPUT", 100.0)
    plant.pulse("condenser", "START")
    plant.write("condensate_pump", "MANUAL_OUTPUT", 20.0)
    plant.pulse("condensate_pump", "START")
    plant.write("feedwater_pump", "MANUAL_OUTPUT", 20.0)
    plant.pulse("feedwater_pump", "START")
    _run_controlled(plant, 40.0, load_mw, boiler_started=False)

    plant.pulse("boiler", "START")
    plant.pulse("steam_valve", "START")
    plant.pulse("turbine", "START")
    plant.pulse("generator", "START")
    _run_controlled(plant, seconds, load_mw)


def _run_controlled(plant: MiniPlant, seconds: float, load_mw: float,
                    boiler_started: bool = True) -> None:
    for _ in range(int(seconds * 2)):     # 每 0.5 秒執行一次簡化控制
        simple_control(plant, load_mw, boiler_started)
        plant.step(5)


def simple_control(plant: MiniPlant, load_mw: float, boiler_started: bool = True) -> None:
    """測試用的簡化控制器（與 DCS 邏輯獨立，避免測試依賴 Modbus）。"""
    boiler = plant.dev("boiler")
    valve = plant.dev("steam_valve")
    turbine = plant.dev("turbine")
    generator = plant.dev("generator")
    fw_pump = plant.dev("feedwater_pump")
    cd_pump = plant.dev("condensate_pump")
    tank = plant.dev("feedwater_tank")

    # 壓力 -> 燃燒器（含蒸汽流量前饋；升壓期間限制輸出避免超壓，達壓後解除）
    if boiler.pressure > 95.0:
        plant.pressure_ramp_done = True
    if boiler_started and not boiler.sm.tripped:
        burner = 0.9 * boiler.steam_outflow + 1.0 * (100.0 - boiler.pressure)
        cap = 100.0 if getattr(plant, "pressure_ramp_done", False) else 20.0
        plant.write("boiler", "MANUAL_OUTPUT", max(0.0, min(cap, burner)))

    # 三元素水位 -> 給水泵
    speed = 100.0 * boiler.evaporation / 120.0 + 2.0 * (66.7 - boiler.level_indicated)
    plant.write("feedwater_pump", "MANUAL_OUTPUT", max(0.0, min(100.0, speed)))

    # 給水槽水位 -> 凝結水泵
    cd_speed = 100.0 * fw_pump.flow / 120.0 + 1.5 * (60.0 - tank.level)
    plant.write("condensate_pump", "MANUAL_OUTPUT", max(0.0, min(100.0, cd_speed)))

    # 轉速 -> 主蒸汽閥（升速期間限制開度，避免瞬間超速）
    limit = 3.0 if generator.breaker_closed else 2.0
    delta = max(-limit, min(limit, 0.01 * (3000.0 - turbine.speed_rpm)
                            + 0.05 * (generator.electrical_power - turbine.mech_power)))
    position = max(0.0, min(100.0, valve.hr("MANUAL_OUTPUT") + delta))
    if turbine.speed_rpm < 2900.0 and not generator.breaker_closed:
        position = min(position, 15.0)
    plant.write("steam_valve", "MANUAL_OUTPUT", position)

    # 併聯與負載（併聯後緩慢加載，避免蒸汽供應跟不上）
    if not generator.breaker_closed and abs(turbine.speed_rpm - 3000.0) < 25.0 \
            and abs(generator.phase_angle) < 10.0 and generator.sm.running \
            and boiler.pressure > 90.0:
        plant.pulse("generator", "BREAKER_CLOSE")
    if generator.breaker_closed:
        setpoint = min(load_mw, generator.hr("PRIMARY_SETPOINT") + 0.25)
    else:
        setpoint = 0.0
    plant.write("generator", "PRIMARY_SETPOINT", setpoint)
