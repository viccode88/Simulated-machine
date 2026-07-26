"""設備狀態機。"""
from __future__ import annotations

from typing import Callable, Iterable

from ..modbus.register_map import DeviceState

# 預設允許轉換。設備可在 build 時擴充（例如鍋爐的 PURGING / IGNITING / PRESSURIZING）。
DEFAULT_TRANSITIONS: dict[DeviceState, set[DeviceState]] = {
    # 閥門、發電機勵磁等「瞬時可用」設備允許 OFF -> RUNNING
    DeviceState.OFF: {DeviceState.STARTING, DeviceState.RUNNING, DeviceState.TRIPPED,
                      DeviceState.MAINTENANCE},
    DeviceState.STARTING: {DeviceState.RUNNING, DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF},
    DeviceState.RUNNING: {DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF},
    DeviceState.STOPPING: {DeviceState.OFF, DeviceState.TRIPPED},
    DeviceState.TRIPPED: {DeviceState.OFF},  # 只能經由 reset 回到 OFF
    DeviceState.MAINTENANCE: {DeviceState.OFF, DeviceState.TRIPPED},
    DeviceState.SAFE_HOLD: {DeviceState.OFF, DeviceState.TRIPPED, DeviceState.RUNNING},
}

BOILER_TRANSITIONS: dict[DeviceState, set[DeviceState]] = {
    **DEFAULT_TRANSITIONS,
    DeviceState.OFF: {DeviceState.PURGING, DeviceState.TRIPPED, DeviceState.MAINTENANCE},
    DeviceState.PURGING: {DeviceState.IGNITING, DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF},
    DeviceState.IGNITING: {DeviceState.PRESSURIZING, DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF},
    DeviceState.PRESSURIZING: {DeviceState.RUNNING, DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF},
    DeviceState.RUNNING: {DeviceState.STOPPING, DeviceState.TRIPPED, DeviceState.OFF, DeviceState.PRESSURIZING},
}


class StateMachine:
    def __init__(
        self,
        initial: DeviceState = DeviceState.OFF,
        transitions: dict[DeviceState, set[DeviceState]] | None = None,
        on_change: Callable[[DeviceState, DeviceState, str], None] | None = None,
        on_reject: Callable[[DeviceState, DeviceState, str], None] | None = None,
    ) -> None:
        self._state = initial
        self._transitions = transitions or DEFAULT_TRANSITIONS
        self._on_change = on_change
        self._on_reject = on_reject
        self.time_in_state = 0.0
        self.previous: DeviceState = initial

    @property
    def state(self) -> DeviceState:
        return self._state

    def tick(self, dt: float) -> None:
        self.time_in_state += dt

    def can(self, target: DeviceState) -> bool:
        if target is self._state:
            return True
        return target in self._transitions.get(self._state, set())

    def to(self, target: DeviceState, reason: str = "") -> bool:
        if target is self._state:
            return True
        if not self.can(target):
            if self._on_reject:
                self._on_reject(self._state, target, reason)
            return False
        self.previous, self._state = self._state, target
        self.time_in_state = 0.0
        if self._on_change:
            self._on_change(self.previous, target, reason)
        return True

    def force(self, target: DeviceState, reason: str = "") -> None:
        """保護動作專用：無視轉換表（跳機永遠可以發生）。"""
        if target is self._state:
            return
        self.previous, self._state = self._state, target
        self.time_in_state = 0.0
        if self._on_change:
            self._on_change(self.previous, target, reason)

    # -- 查詢 --------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._state in (DeviceState.RUNNING,)

    @property
    def starting(self) -> bool:
        return self._state in (
            DeviceState.STARTING,
            DeviceState.PURGING,
            DeviceState.IGNITING,
            DeviceState.PRESSURIZING,
        )

    @property
    def stopping(self) -> bool:
        return self._state is DeviceState.STOPPING

    @property
    def tripped(self) -> bool:
        return self._state is DeviceState.TRIPPED

    def in_any(self, states: Iterable[DeviceState]) -> bool:
        return self._state in set(states)

    # -- 快照 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "state": int(self._state),
            "previous": int(self.previous),
            "time_in_state": self.time_in_state,
        }

    def from_dict(self, data: dict) -> None:
        self._state = DeviceState(int(data.get("state", DeviceState.OFF)))
        self.previous = DeviceState(int(data.get("previous", DeviceState.OFF)))
        self.time_in_state = float(data.get("time_in_state", 0.0))
