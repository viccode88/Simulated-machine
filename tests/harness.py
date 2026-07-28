"""測試用的行程內迷你機組：不需要 docker 也能跑完整 lockstep 物理迴圈。

設備是自持的，因此這個 harness 不含任何控制器：只要推進時間，機組就會自己
啟動、併聯並維持負載。`kick_watchdog=False` 可以模擬「完全沒有 PLC」的情況。
"""
from __future__ import annotations

import os
import tempfile

from common.modbus.register_map import Table
from common.simbus.protocol import SignalValue

from devices.registry import DEVICE_CLASSES

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs")


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


def bring_to_steady(plant: MiniPlant, load_mw: float = 60.0, seconds: float = 1200.0) -> None:
    """把機組帶到接近穩態。

    設備自持：不需要任何控制器介入，只要給它時間。負載目標寫進發電機的
    40010 PRIMARY_SETPOINT（SCADA 也只能做到這個層級的設定）。
    """
    plant.write("generator", "PRIMARY_SETPOINT", load_mw)
    plant.run_seconds(seconds)


def run_until(plant: MiniPlant, predicate, timeout_s: float = 1200.0,
              step_ticks: int = 5, **kwargs) -> float:
    """推進模擬直到 predicate(plant) 成立，回傳耗費的模擬秒數（逾時則回傳 -1）。"""
    elapsed = 0.0
    while elapsed < timeout_s:
        if predicate(plant):
            return elapsed
        plant.step(step_ticks, **kwargs)
        elapsed += step_ticks * plant.dt
    return elapsed if predicate(plant) else -1.0
