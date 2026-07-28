"""設備內建的本地調節器（自持控制用）。

每台設備自己維持自己的程序量，因此調節器屬於設備框架的一部分，
不再是外部控制器的東西。功能刻意保持最小但完整：

* PI(D)，微分作用於 PV（設定值變動不會造成微分尖峰）
* anti-windup：輸出飽和且誤差方向會加深飽和時停止積分
* 輸出速率限制、死區、輸出上下限（上限可逐次掃描動態限幅）
* ``track()``：手動／安全動作期間讓積分項追隨實際輸出，回到自動時不跳變
"""
from __future__ import annotations

from dataclasses import dataclass

from ..util import clamp, rate_limit


@dataclass
class Regulator:
    name: str
    kp: float = 1.0
    ki: float = 0.0
    kd: float = 0.0
    setpoint: float = 0.0
    out_min: float = 0.0
    out_max: float = 100.0
    rate_up: float = 100.0        # 單位/秒
    rate_down: float = 100.0
    deadband: float = 0.0
    integral_limit: float = 100.0
    direct_acting: bool = True    # True：PV 低於 SP -> 輸出上升

    integral: float = 0.0
    output: float = 0.0
    raw_output: float = 0.0
    last_pv: float = 0.0
    last_error: float = 0.0
    saturated: bool = False
    _initialised: bool = False
    _track_pending: bool = False

    # -- 主要更新 ----------------------------------------------------------
    def update(self, pv: float, dt: float, feedforward: float = 0.0,
               out_max: float | None = None, out_min: float | None = None) -> float:
        """執行一次掃描並回傳輸出。

        ``out_max`` / ``out_min`` 是本次掃描的動態限幅（例如升壓期間的燃燒器上限）。
        限幅必須進到 PID 內部而不是事後 min()：從外面砍會讓積分項一路累積到
        integral_limit，限幅解除的瞬間輸出跳到滿檔。
        """
        if dt <= 0:
            return self.output
        high = self.out_max if out_max is None else min(self.out_max, out_max)
        low = self.out_min if out_min is None else max(self.out_min, out_min)
        if high < low:
            high = low
        if not self._initialised:
            self.last_pv = pv
            self._initialised = True

        error = self.setpoint - pv
        if not self.direct_acting:
            error = -error
        if abs(error) <= self.deadband:
            error = 0.0

        proportional = self.kp * error
        derivative = -self.kd * (pv - self.last_pv) / dt
        if not self.direct_acting:
            derivative = -derivative

        if self._track_pending:
            # 由手動／安全動作回到自動：以目前輸出反推積分項
            self.integral = clamp(self.output - proportional - derivative - feedforward,
                                  -self.integral_limit, self.integral_limit)
            self._track_pending = False

        candidate = proportional + self.integral + derivative + feedforward
        if not ((candidate >= high and error > 0) or (candidate <= low and error < 0)):
            self.integral = clamp(self.integral + self.ki * error * dt,
                                  -self.integral_limit, self.integral_limit)

        self.raw_output = proportional + self.integral + derivative + feedforward
        target = clamp(self.raw_output, low, high)
        self.saturated = target != self.raw_output
        self.output = rate_limit(self.output, target, self.rate_up, self.rate_down, dt)
        self.output = clamp(self.output, low, high)
        self.last_error = error
        self.last_pv = pv
        return self.output

    # -- 追隨與強制 --------------------------------------------------------
    def track(self, value: float) -> None:
        """手動模式或安全邏輯期間追隨實際輸出，下一次自動掃描不跳變。"""
        self.output = clamp(value, self.out_min, self.out_max)
        self.raw_output = self.output
        self.integral = clamp(self.output, -self.integral_limit, self.integral_limit)
        self._track_pending = True

    def hold(self, value: float = 0.0) -> float:
        """安全邏輯優先：直接把輸出壓到指定值（例如跳機時關閥）。"""
        self.track(value)
        return self.output

    def reset(self) -> None:
        self.integral = 0.0
        self.output = 0.0
        self.raw_output = 0.0
        self.last_error = 0.0
        self.saturated = False
        self._initialised = False
        self._track_pending = False

    # -- 快照 --------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "setpoint": self.setpoint,
            "integral": self.integral,
            "output": self.output,
            "last_pv": self.last_pv,
            "last_error": self.last_error,
            "initialised": self._initialised,
        }

    def from_dict(self, data: dict) -> None:
        for key in ("setpoint", "integral", "output", "last_pv", "last_error"):
            if key in data:
                setattr(self, key, float(data[key]))
        self.raw_output = self.output
        self._initialised = bool(data.get("initialised", self._initialised))
        self._track_pending = False


def build_regulator(name: str, config: dict, **defaults) -> Regulator:
    """由 YAML 區塊建立調節器；缺項用 defaults，再缺用 dataclass 預設值。"""
    values = dict(defaults)
    values.update({key: value for key, value in (config or {}).items()
                   if key in Regulator.__dataclass_fields__ and key != "name"})
    return Regulator(name=name, **{key: (bool(value) if key == "direct_acting" else float(value))
                                   for key, value in values.items()})
