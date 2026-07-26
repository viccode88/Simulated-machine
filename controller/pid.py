"""PID 控制器：anti-windup、bumpless transfer、輸出速率限制、死區、前饋。"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.util import clamp, rate_limit


@dataclass
class PID:
    name: str
    kp: float = 1.0
    ki: float = 0.0
    kd: float = 0.0
    setpoint: float = 0.0
    out_min: float = 0.0
    out_max: float = 100.0
    rate_up: float = 100.0      # 單位/秒
    rate_down: float = 100.0
    deadband: float = 0.0
    integral_limit: float = 100.0
    direct_acting: bool = True  # True: PV 高於 SP -> 輸出下降（反作用需設 False）
    auto: bool = False

    integral: float = 0.0
    last_error: float = 0.0
    last_pv: float = 0.0
    output: float = 0.0
    raw_output: float = 0.0
    manual_output: float = 0.0
    saturated: bool = False
    _initialised: bool = False
    _bumpless_pending: bool = False

    # -- 主要更新 ----------------------------------------------------------
    def update(self, pv: float, dt: float, feedforward: float = 0.0) -> float:
        if dt <= 0:
            return self.output
        if not self._initialised:
            self.last_pv = pv
            self._initialised = True

        if not self.auto:
            # 手動：bumpless transfer 準備 — 讓積分項追隨實際輸出
            self.output = clamp(self.manual_output, self.out_min, self.out_max)
            self.integral = clamp(self.output - feedforward, -self.integral_limit,
                                  self.integral_limit)
            self.last_error = self.setpoint - pv
            self.last_pv = pv
            self.raw_output = self.output
            return self.output

        error = self.setpoint - pv
        if not self.direct_acting:
            error = -error
        if abs(error) <= self.deadband:
            error = 0.0

        proportional = self.kp * error
        derivative = -self.kd * (pv - self.last_pv) / dt  # 微分作用於 PV，避免 SP 跳動
        if not self.direct_acting:
            derivative = -derivative

        if self._bumpless_pending:
            # bumpless transfer：以目前輸出反推積分項，切換瞬間輸出不跳變
            self.integral = clamp(self.output - proportional - derivative - feedforward,
                                  -self.integral_limit, self.integral_limit)
            self._bumpless_pending = False

        candidate = proportional + self.integral + derivative + feedforward
        # anti-windup：僅在輸出未飽和、或誤差方向會讓輸出離開飽和時才積分
        if not (
            (candidate >= self.out_max and error > 0) or (candidate <= self.out_min and error < 0)
        ):
            self.integral += self.ki * error * dt
            self.integral = clamp(self.integral, -self.integral_limit, self.integral_limit)

        self.raw_output = proportional + self.integral + derivative + feedforward
        target = clamp(self.raw_output, self.out_min, self.out_max)
        self.saturated = target != self.raw_output
        self.output = rate_limit(self.output, target, self.rate_up, self.rate_down, dt)
        self.last_error = error
        self.last_pv = pv
        return self.output

    # -- 模式切換 ----------------------------------------------------------
    def to_auto(self, current_output: float | None = None) -> None:
        if self.auto:
            return
        if current_output is not None:
            self.output = clamp(current_output, self.out_min, self.out_max)
        self._bumpless_pending = True
        self.auto = True

    def to_manual(self, output: float | None = None) -> None:
        self.manual_output = self.output if output is None else output
        self.auto = False

    def force_output(self, value: float) -> None:
        """安全邏輯優先：直接把輸出壓到指定值並同步積分項。"""
        self.output = clamp(value, self.out_min, self.out_max)
        self.integral = clamp(self.output, -self.integral_limit, self.integral_limit)
        self.raw_output = self.output
        self._bumpless_pending = True  # 下一次掃描重新對齊積分項

    # -- 快照 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "kp": self.kp, "ki": self.ki, "kd": self.kd, "setpoint": self.setpoint,
            "integral": self.integral, "last_error": self.last_error, "last_pv": self.last_pv,
            "output": self.output, "auto": self.auto, "manual_output": self.manual_output,
            "initialised": self._initialised,
        }

    def from_dict(self, data: dict) -> None:
        for key in ("kp", "ki", "kd", "setpoint", "integral", "last_error", "last_pv",
                    "output", "manual_output"):
            if key in data:
                setattr(self, key, float(data[key]))
        self.auto = bool(data.get("auto", self.auto))
        self._bumpless_pending = False
        self._initialised = bool(data.get("initialised", self._initialised))


@dataclass
class ThreeElementLevel:
    """三元素鍋爐水位控制：水位修正 + 蒸汽流量前饋 - 給水流量回授。"""

    level_pid: PID
    flow_pid: PID
    feedforward_gain: float = 1.0
    enabled: bool = True
    history: list[float] = field(default_factory=list)

    def update(self, level_pv: float, steam_flow: float, feedwater_flow: float, dt: float,
               rated_flow: float = 120.0) -> float:
        level_trim = self.level_pid.update(level_pv, dt)
        if not self.enabled:
            return level_trim
        # 蒸汽流量前饋（換算成泵浦速度百分比）
        feedforward = self.feedforward_gain * 100.0 * steam_flow / max(1.0, rated_flow)
        # 給水流量回授：以流量誤差修正
        flow_setpoint = clamp(level_trim + feedforward, 0.0, 150.0)
        self.flow_pid.setpoint = flow_setpoint
        measured = 100.0 * feedwater_flow / max(1.0, rated_flow)
        return self.flow_pid.update(measured, dt)

    def to_dict(self) -> dict:
        return {"level": self.level_pid.to_dict(), "flow": self.flow_pid.to_dict(),
                "enabled": self.enabled}

    def from_dict(self, data: dict) -> None:
        self.level_pid.from_dict(data.get("level") or {})
        self.flow_pid.from_dict(data.get("flow") or {})
        self.enabled = bool(data.get("enabled", self.enabled))
