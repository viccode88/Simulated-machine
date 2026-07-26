"""保護、鎖存與第一故障原因。

規格重點：
* 跳機具有 active / latched / first_out / resettable 四個屬性。
* 條件消失後 active=false、latched=true；只有重置命令與安全條件成立才能解除。
* 第一故障原因不可被後續連鎖跳機覆蓋，並記錄前後 10 秒主要變數。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable

FIRST_OUT_PRE_SECONDS = 10.0
FIRST_OUT_POST_SECONDS = 10.0
FIRST_OUT_SAMPLE_PERIOD = 0.5


@dataclass
class ProtectionSpec:
    """單一保護。門檻一律來自 YAML，不硬編碼。"""

    code: int
    name: str
    signal: str                      # 從 values dict 取出的鍵
    direction: str = "high"          # high / low
    alarm_threshold: float | None = None
    trip_threshold: float | None = None
    delay: float = 0.0
    reset_threshold: float | None = None
    reset_delay: float = 5.0
    alarm_code: int | None = None
    enabled: bool = True
    trips: bool = True               # False = 只警報
    message: str = ""
    inhibit: Callable[[], bool] | None = None  # 回傳 True 時不評估（例如設備停機）

    def exceeded(self, value: float, threshold: float) -> bool:
        return value > threshold if self.direction == "high" else value < threshold

    def cleared(self, value: float, threshold: float) -> bool:
        return value <= threshold if self.direction == "high" else value >= threshold


@dataclass
class ProtectionState:
    active: bool = False
    latched: bool = False
    first_out: bool = False
    resettable: bool = False
    above_time: float = 0.0
    clear_time: float = 0.0
    trip_count: int = 0
    last_value: float = 0.0


@dataclass
class FirstOut:
    code: int = 0
    name: str = ""
    device: str = ""
    sim_time: float = 0.0
    wall_time: str = ""
    value: float = 0.0
    threshold: float = 0.0
    control_output: float = 0.0
    pre_trend: list[dict] = field(default_factory=list)
    post_trend: list[dict] = field(default_factory=list)
    complete: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name, "device": self.device,
            "sim_time": self.sim_time, "wall_time": self.wall_time,
            "value": self.value, "threshold": self.threshold,
            "control_output": self.control_output,
            "pre_trend": self.pre_trend, "post_trend": self.post_trend,
            "complete": self.complete,
        }

    @staticmethod
    def from_dict(data: dict) -> "FirstOut":
        record = FirstOut()
        record.__dict__.update({k: v for k, v in data.items() if k in record.__dict__})
        return record


class ProtectionEngine:
    def __init__(
        self,
        device: str,
        specs: list[ProtectionSpec],
        emit: Callable[..., None] | None = None,
        alarm_hook: Callable[[int, bool, float, float], None] | None = None,
        wall_time_fn: Callable[[], str] | None = None,
    ) -> None:
        self.device = device
        self.specs = {spec.code: spec for spec in specs}
        self.states: dict[int, ProtectionState] = {spec.code: ProtectionState() for spec in specs}
        self._emit = emit
        self._alarm_hook = alarm_hook
        self._wall_time = wall_time_fn or (lambda: "")
        self.first_out: FirstOut | None = None
        self.trip_count = 0
        self._trend: deque[dict] = deque(maxlen=int(FIRST_OUT_PRE_SECONDS / FIRST_OUT_SAMPLE_PERIOD) + 2)
        self._trend_timer = 0.0
        self._post_timer = 0.0
        self.bit_of = {spec.code: index for index, spec in enumerate(specs) if index < 16}

    # -- 評估 --------------------------------------------------------------
    def evaluate(self, dt: float, values: dict[str, float], sim_time: float,
                 control_output: float = 0.0) -> list[int]:
        """回傳本次新觸發的跳機碼列表。"""
        self._sample_trend(dt, values, sim_time)
        new_trips: list[int] = []
        for code, spec in self.specs.items():
            if not spec.enabled:
                continue
            state = self.states[code]
            if spec.inhibit is not None and spec.inhibit():
                # 被抑制時不評估，但仍讓鎖存可以走向「可重置」
                state.above_time = 0.0
                if state.active:
                    state.active = False
                    if self._emit:
                        self._emit("TRIP_CONDITION_CLEARED", code=code, name=spec.name,
                                   value=state.last_value, inhibited=True)
                state.clear_time += dt
                state.resettable = state.clear_time >= spec.reset_delay
                continue
            value = values.get(spec.signal)
            if value is None:
                continue
            state.last_value = value

            # 警報
            if spec.alarm_threshold is not None and self._alarm_hook and spec.alarm_code:
                self._alarm_hook(spec.alarm_code, spec.exceeded(value, spec.alarm_threshold),
                                 value, spec.alarm_threshold)

            if spec.trip_threshold is None or not spec.trips:
                continue

            if spec.exceeded(value, spec.trip_threshold):
                state.above_time += dt
                state.clear_time = 0.0
                if state.above_time >= spec.delay and not state.active:
                    state.active = True
                    was_first = self.first_out is None
                    if not state.latched:
                        state.trip_count += 1
                        self.trip_count += 1
                    state.latched = True
                    state.first_out = state.first_out or was_first
                    new_trips.append(code)
                    if was_first:
                        self._capture_first_out(spec, value, sim_time, control_output)
                    if self._emit:
                        self._emit("TRIP_LATCHED", code=code, name=spec.name,
                                   first_out=was_first, value=value,
                                   threshold=spec.trip_threshold, message=spec.message)
            else:
                state.above_time = 0.0
                # active：跳機條件是否仍存在（已回到 trip 門檻另一側即消失）
                if state.active:
                    state.active = False
                    if self._emit:
                        self._emit("TRIP_CONDITION_CLEARED", code=code, name=spec.name,
                                   value=value, threshold=spec.trip_threshold)
                # resettable：遲滯條件，必須回到 reset_threshold 之外並持續 reset_delay
                reset_threshold = (
                    spec.reset_threshold if spec.reset_threshold is not None else spec.trip_threshold
                )
                if spec.cleared(value, reset_threshold):
                    state.clear_time += dt
                else:
                    state.clear_time = 0.0
                state.resettable = state.clear_time >= spec.reset_delay
        self._finish_first_out(dt, values, sim_time)
        return new_trips

    # -- 手動/外部跳機 ------------------------------------------------------
    def force_trip(self, code: int, sim_time: float, value: float = 0.0,
                   control_output: float = 0.0, message: str = "") -> None:
        spec = self.specs.get(code)
        state = self.states.get(code)
        if spec is None or state is None:
            return
        if state.latched and state.active:
            return
        was_first = self.first_out is None
        state.active = True
        state.latched = True
        state.first_out = state.first_out or was_first
        state.trip_count += 1
        self.trip_count += 1
        if was_first:
            self._capture_first_out(spec, value, sim_time, control_output)
        if self._emit:
            self._emit("TRIP_LATCHED", code=code, name=spec.name, first_out=was_first,
                       value=value, threshold=spec.trip_threshold,
                       message=message or spec.message)

    # -- 鎖存查詢 ----------------------------------------------------------
    @property
    def any_latched(self) -> bool:
        return any(s.latched for s in self.states.values())

    @property
    def any_active(self) -> bool:
        return any(s.active for s in self.states.values())

    def trip_word(self) -> int:
        word = 0
        for code, state in self.states.items():
            if state.latched and code in self.bit_of:
                word |= 1 << self.bit_of[code]
        return word & 0xFFFF

    def first_out_code(self) -> int:
        return self.first_out.code if self.first_out else 0

    def can_reset(self) -> tuple[bool, str]:
        for code, state in self.states.items():
            if state.latched and state.active:
                return False, f"{self.specs[code].name} 仍在動作中"
            if state.latched and not state.resettable:
                return False, f"{self.specs[code].name} 尚未滿足重置條件"
        return True, ""

    def reset(self) -> bool:
        allowed, _ = self.can_reset()
        if not allowed:
            return False
        for state in self.states.values():
            state.latched = False
            state.first_out = False
            state.above_time = 0.0
            state.clear_time = 0.0
            state.resettable = False
        if self.first_out and self._emit:
            self._emit("FIRST_OUT_RESET", code=self.first_out.code, name=self.first_out.name)
        self.first_out = None
        return True

    def latched_list(self) -> list[dict]:
        return [
            {
                "code": code,
                "name": self.specs[code].name,
                "active": s.active,
                "latched": s.latched,
                "first_out": s.first_out,
                "resettable": s.resettable,
                "value": s.last_value,
                "threshold": self.specs[code].trip_threshold,
                "message": self.specs[code].message,
            }
            for code, s in self.states.items()
            if s.latched or s.active
        ]

    # -- 第一故障趨勢 -------------------------------------------------------
    def _sample_trend(self, dt: float, values: dict[str, float], sim_time: float) -> None:
        self._trend_timer += dt
        if self._trend_timer < FIRST_OUT_SAMPLE_PERIOD:
            return
        self._trend_timer = 0.0
        sample = {"sim_time": round(sim_time, 2)}
        sample.update({k: round(float(v), 4) for k, v in values.items() if isinstance(v, (int, float))})
        self._trend.append(sample)
        if self.first_out and not self.first_out.complete:
            self.first_out.post_trend.append(sample)

    def _capture_first_out(self, spec: ProtectionSpec, value: float, sim_time: float,
                           control_output: float) -> None:
        self.first_out = FirstOut(
            code=spec.code,
            name=spec.name,
            device=self.device,
            sim_time=round(sim_time, 3),
            wall_time=self._wall_time(),
            value=value,
            threshold=spec.trip_threshold if spec.trip_threshold is not None else 0.0,
            control_output=control_output,
            pre_trend=list(self._trend),
        )
        self._post_timer = 0.0
        if self._emit:
            self._emit("FIRST_OUT", code=spec.code, name=spec.name, value=value,
                       threshold=spec.trip_threshold, message=spec.message,
                       control_output=control_output)

    def _finish_first_out(self, dt: float, values: dict[str, float], sim_time: float) -> None:
        if not self.first_out or self.first_out.complete:
            return
        self._post_timer += dt
        if self._post_timer >= FIRST_OUT_POST_SECONDS:
            self.first_out.complete = True
            if self._emit:
                self._emit("FIRST_OUT_TREND_COMPLETE", code=self.first_out.code,
                           name=self.first_out.name,
                           samples=len(self.first_out.pre_trend) + len(self.first_out.post_trend))

    # -- 快照/持久化 --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "trip_count": self.trip_count,
            "first_out": self.first_out.to_dict() if self.first_out else None,
            "states": {
                str(code): {
                    "active": s.active, "latched": s.latched, "first_out": s.first_out,
                    "resettable": s.resettable, "above_time": s.above_time,
                    "clear_time": s.clear_time, "trip_count": s.trip_count,
                    "last_value": s.last_value,
                }
                for code, s in self.states.items()
            },
            "trend": list(self._trend),
        }

    def from_dict(self, data: dict) -> None:
        self.trip_count = int(data.get("trip_count", 0))
        first_out = data.get("first_out")
        self.first_out = FirstOut.from_dict(first_out) if first_out else None
        for code_str, values in (data.get("states") or {}).items():
            code = int(code_str)
            if code not in self.states:
                continue
            state = self.states[code]
            state.active = bool(values.get("active", False))
            state.latched = bool(values.get("latched", False))
            state.first_out = bool(values.get("first_out", False))
            state.resettable = bool(values.get("resettable", False))
            state.above_time = float(values.get("above_time", 0.0))
            state.clear_time = float(values.get("clear_time", 0.0))
            state.trip_count = int(values.get("trip_count", 0))
            state.last_value = float(values.get("last_value", 0.0))
        self._trend.clear()
        for sample in data.get("trend") or []:
            self._trend.append(sample)

    def clear_all_latches(self) -> None:
        """僅供 snapshot restore 的 clean 模式使用。"""
        for state in self.states.values():
            state.active = False
            state.latched = False
            state.first_out = False
            state.above_time = 0.0
            state.clear_time = 0.0
            state.resettable = False
        self.first_out = None


def build_protections(device: str, cfg: dict, definitions: list[dict]) -> list[ProtectionSpec]:
    """由 YAML 的 protections 區段建立 ProtectionSpec。

    definitions 為設備程式提供的預設骨架（code/name/signal/direction/alarm_code/message），
    門檻與延遲一律取自設定檔。
    """
    configured = cfg.get("protections") or {}
    specs: list[ProtectionSpec] = []
    for item in definitions:
        entry = configured.get(item["name"], {}) or {}
        if entry.get("enabled") is False:
            continue
        specs.append(
            ProtectionSpec(
                code=item["code"],
                name=item["name"],
                signal=item["signal"],
                direction=item.get("direction", "high"),
                alarm_threshold=entry.get("alarm"),
                trip_threshold=entry.get("trip"),
                delay=float(entry.get("delay", item.get("delay", 0.0))),
                reset_threshold=entry.get("reset"),
                reset_delay=float(entry.get("reset_delay", 5.0)),
                alarm_code=item.get("alarm_code"),
                trips=entry.get("trip") is not None and item.get("trips", True),
                message=item.get("message", ""),
            )
        )
    return specs
