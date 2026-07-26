"""跳機矩陣：設備跳機 -> 全廠連鎖動作。

設備本身已透過 sim_net 訊號互鎖（例如汽輪機跳機 -> 主蒸汽閥快關），
這裡是 DCS 層額外的動作，確保即使某條互鎖失效仍有第二層防護。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class TripAction:
    device: str
    action: str          # write_holding / pulse_coil
    target: str          # 暫存器名稱
    value: float = 0.0
    reason: str = ""
    source: str = ""     # 觸發此動作的來源設備，由 TripMatrix 於建構時填入

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.device, self.action, self.target)


@dataclass
class TripRule:
    source: str          # 來源設備
    description: str
    actions: list[TripAction] = field(default_factory=list)


DEFAULT_MATRIX: list[TripRule] = [
    TripRule(
        source="turbine",
        description="汽輪機跳機 -> 斷路器立即打開、主蒸汽閥快速關閉",
        actions=[
            TripAction("generator", "pulse_coil", "BREAKER_OPEN", reason="TURBINE_TRIP"),
            TripAction("steam_valve", "write_holding", "MANUAL_OUTPUT", 0.0, "TURBINE_TRIP"),
            TripAction("generator", "write_holding", "PRIMARY_SETPOINT", 0.0, "TURBINE_TRIP"),
        ],
    ),
    TripRule(
        source="boiler",
        description="鍋爐跳機 -> 燃燒器歸零、主蒸汽閥關閉",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "BOILER_TRIP"),
            TripAction("steam_valve", "write_holding", "MANUAL_OUTPUT", 0.0, "BOILER_TRIP"),
        ],
    ),
    TripRule(
        source="condenser",
        description="冷凝器高壓 -> 降載",
        actions=[
            TripAction("generator", "write_holding", "PRIMARY_SETPOINT", 0.0, "CONDENSER_TRIP"),
        ],
    ),
    TripRule(
        source="feedwater_pump",
        description="給水泵跳機 -> 降低燃燒率，避免鍋爐低水位",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "FEEDWATER_PUMP_TRIP"),
        ],
    ),
    TripRule(
        source="condensate_pump",
        description="凝結水泵跳機 -> 給水泵降速，保護給水槽",
        actions=[
            TripAction("feedwater_pump", "write_holding", "MANUAL_OUTPUT", 20.0,
                       "CONDENSATE_PUMP_TRIP"),
        ],
    ),
    TripRule(
        source="steam_valve",
        description="主蒸汽閥故障 -> 停止燃燒",
        actions=[
            TripAction("boiler", "write_holding", "MANUAL_OUTPUT", 0.0, "STEAM_VALVE_FAULT"),
        ],
    ),
]


class TripMatrix:
    """跳機矩陣。

    動作必須由執行端以 confirm() 回報結果：跳機當下 Modbus 寫入失敗是常見情況
    （設備忙碌、租約衝突、連線瞬斷），若只在跳機邊緣觸發一次就把來源標記為
    已處理，這層連鎖保護會被永久漏掉。未確認成功的動作會在下一次 evaluate 重試。
    """

    def __init__(self, rules: list[TripRule] | None = None,
                 emit: Callable[..., None] | None = None) -> None:
        # 複製規則並補上 source，避免修改共用的 DEFAULT_MATRIX
        source_rules = rules if rules is not None else DEFAULT_MATRIX
        self.rules = [
            dataclasses.replace(
                rule,
                actions=[dataclasses.replace(a, source=rule.source) for a in rule.actions],
            )
            for rule in source_rules
        ]
        self._emit = emit
        self._fired: set[str] = set()
        self._pending: dict[str, list[TripAction]] = {}
        self._attempts: dict[tuple[str, str, str], int] = {}

    def evaluate(self, tripped: dict[str, bool]) -> list[TripAction]:
        """回傳需要執行的動作：跳機邊緣的新動作，加上先前尚未確認成功的重試。"""
        actions: list[TripAction] = []
        for rule in self.rules:
            active = bool(tripped.get(rule.source, False))
            if active:
                if rule.source not in self._fired:
                    self._fired.add(rule.source)
                    self._pending[rule.source] = list(rule.actions)
                    if self._emit:
                        self._emit("TRIP_MATRIX_FIRED", source=rule.source,
                                   description=rule.description,
                                   actions=[a.target for a in rule.actions])
                actions.extend(self._pending.get(rule.source, ()))
            elif rule.source in self._fired:
                self._fired.discard(rule.source)
                for action in self._pending.pop(rule.source, []):
                    self._attempts.pop(action.key, None)
                if self._emit:
                    self._emit("TRIP_MATRIX_CLEARED", source=rule.source)
        return actions

    def confirm(self, action: TripAction, ok: bool) -> None:
        """由執行端回報動作結果；失敗的動作保留在待辦中，下一次 evaluate 重試。"""
        pending = self._pending.get(action.source)
        if pending is None:
            return
        if ok:
            self._attempts.pop(action.key, None)
            remaining = [a for a in pending if a.key != action.key]
            if remaining:
                self._pending[action.source] = remaining
            else:
                self._pending.pop(action.source, None)
            return
        attempts = self._attempts.get(action.key, 0) + 1
        self._attempts[action.key] = attempts
        if self._emit and (attempts in (1, 5, 20) or attempts % 50 == 0):
            self._emit("TRIP_MATRIX_ACTION_FAILED", source=action.source, device=action.device,
                       target=action.target, action=action.action, reason=action.reason,
                       attempts=attempts)

    @property
    def pending_actions(self) -> int:
        return sum(len(v) for v in self._pending.values())

    def to_dict(self) -> dict:
        return {
            "fired": sorted(self._fired),
            "pending": {
                source: [self.rules_index(source, a) for a in actions]
                for source, actions in self._pending.items()
            },
        }

    def rules_index(self, source: str, action: TripAction) -> int:
        for rule in self.rules:
            if rule.source != source:
                continue
            for index, candidate in enumerate(rule.actions):
                if candidate.key == action.key:
                    return index
        return -1

    def from_dict(self, data: dict) -> None:
        self._fired = set(data.get("fired") or [])
        self._attempts.clear()
        self._pending = {}
        by_source = {rule.source: rule for rule in self.rules}
        for source, indices in (data.get("pending") or {}).items():
            rule = by_source.get(source)
            if rule is None:
                continue
            actions = [rule.actions[i] for i in indices if 0 <= i < len(rule.actions)]
            if actions:
                self._pending[source] = actions
        # 舊格式快照沒有 pending：已觸發但無待辦，視為全部完成
        for source in self._fired:
            self._pending.setdefault(source, [])
