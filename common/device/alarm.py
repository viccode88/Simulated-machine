"""警報管理：產生、解除、確認，並映射到 Alarm Word 1/2。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class AlarmSpec:
    code: int
    name: str
    bit: int          # 0..31：0-15 -> Alarm Word 1，16-31 -> Alarm Word 2
    message: str = ""

    @property
    def word_index(self) -> int:
        return 0 if self.bit < 16 else 1

    @property
    def word_bit(self) -> int:
        return self.bit % 16


@dataclass
class AlarmState:
    active: bool = False
    latched: bool = False
    acked: bool = False
    value: float | None = None
    threshold: float | None = None
    count: int = 0


class AlarmManager:
    def __init__(self, specs: list[AlarmSpec], emit: Callable[..., None] | None = None) -> None:
        self.specs = {spec.code: spec for spec in specs}
        self.by_name = {spec.name: spec for spec in specs}
        self.states: dict[int, AlarmState] = {spec.code: AlarmState() for spec in specs}
        self._emit = emit
        self.total_count = 0

    def set(self, code: int, active: bool, value: float | None = None,
            threshold: float | None = None) -> None:
        spec = self.specs.get(code)
        if spec is None:
            return
        state = self.states[code]
        state.value = value
        state.threshold = threshold
        if active and not state.active:
            state.active = True
            state.latched = True
            state.acked = False
            state.count += 1
            self.total_count += 1
            if self._emit:
                self._emit("ALARM_SET", code=code, name=spec.name, message=spec.message,
                           value=value, threshold=threshold)
        elif not active and state.active:
            state.active = False
            if self._emit:
                self._emit("ALARM_CLEARED", code=code, name=spec.name, value=value,
                           threshold=threshold)

    def set_by_name(self, name: str, active: bool, value: float | None = None,
                    threshold: float | None = None) -> None:
        spec = self.by_name.get(name)
        if spec:
            self.set(spec.code, active, value, threshold)

    def ack_all(self) -> int:
        acked = 0
        for code, state in self.states.items():
            if state.latched and not state.acked:
                state.acked = True
                acked += 1
                if self._emit:
                    self._emit("ALARM_ACKED", code=code, name=self.specs[code].name)
            if state.latched and not state.active and state.acked:
                state.latched = False
        return acked

    @property
    def any_active(self) -> bool:
        return any(s.active for s in self.states.values())

    @property
    def any_unacked(self) -> bool:
        return any(s.latched and not s.acked for s in self.states.values())

    def words(self) -> tuple[int, int]:
        word1 = word2 = 0
        for code, state in self.states.items():
            if not (state.active or state.latched):
                continue
            spec = self.specs[code]
            if spec.word_index == 0:
                word1 |= 1 << spec.word_bit
            else:
                word2 |= 1 << spec.word_bit
        return word1 & 0xFFFF, word2 & 0xFFFF

    def active_list(self) -> list[dict]:
        return [
            {
                "code": code,
                "name": self.specs[code].name,
                "message": self.specs[code].message,
                "active": state.active,
                "latched": state.latched,
                "acked": state.acked,
                "value": state.value,
                "threshold": state.threshold,
            }
            for code, state in self.states.items()
            if state.active or state.latched
        ]

    # -- 快照/持久化 --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "total_count": self.total_count,
            "states": {
                str(code): {
                    "active": s.active, "latched": s.latched, "acked": s.acked,
                    "value": s.value, "threshold": s.threshold, "count": s.count,
                }
                for code, s in self.states.items()
            },
        }

    def from_dict(self, data: dict) -> None:
        self.total_count = int(data.get("total_count", 0))
        for code_str, values in (data.get("states") or {}).items():
            code = int(code_str)
            if code not in self.states:
                continue
            state = self.states[code]
            state.active = bool(values.get("active", False))
            state.latched = bool(values.get("latched", False))
            state.acked = bool(values.get("acked", False))
            state.value = values.get("value")
            state.threshold = values.get("threshold")
            state.count = int(values.get("count", 0))
