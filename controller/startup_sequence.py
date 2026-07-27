"""冷啟動順序（§14.1）與順序禁止（§14.2）。

每個步驟包含：
    enter  進入步驟時下達的命令
    done   完成條件
    guard  順序禁止條件（不成立則拒絕並停在原步驟）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

Ctx = Any  # DCS 實例


@dataclass
class Step:
    name: str
    description: str
    enter: Callable[[Ctx], Awaitable[None]] | None = None
    done: Callable[[Ctx], bool] = lambda ctx: True
    guard: Callable[[Ctx], tuple[bool, str]] | None = None
    timeout: float = 600.0
    # True：每次 update 都重新評估 guard 並重下命令。
    # 用於「命令可能被設備拒絕、且允許條件是瞬時窗口」的步驟（例如併聯）：
    # 只在進入步驟時下一次命令，錯過窗口就會枯等到逾時。
    repeat_enter: bool = False


async def _noop(ctx: Ctx) -> None:
    return None


def build_sequence() -> list[Step]:
    async def start_cooling(ctx: Ctx) -> None:
        await ctx.write("condenser", "MANUAL_OUTPUT", 100.0)
        await ctx.pulse("condenser", "START")

    async def start_condensate_pump(ctx: Ctx) -> None:
        await ctx.write("condensate_pump", "MANUAL_OUTPUT", 40.0)
        await ctx.pulse("condensate_pump", "START")

    async def tank_to_setpoint(ctx: Ctx) -> None:
        ctx.tank_level.to_auto(ctx.pv("condensate_pump", "ACTUAL_SPEED"))

    async def start_feedwater_pump(ctx: Ctx) -> None:
        await ctx.write("feedwater_pump", "OUTLET_VALVE_CMD", 100.0)
        await ctx.write("feedwater_pump", "MANUAL_OUTPUT", 40.0)
        await ctx.pulse("feedwater_pump", "START")

    async def boiler_level_auto(ctx: Ctx) -> None:
        ctx.boiler_level.level_pid.to_auto(0.0)
        ctx.boiler_level.flow_pid.to_auto(ctx.pv("feedwater_pump", "ACTUAL_SPEED"))

    async def purge(ctx: Ctx) -> None:
        await ctx.pulse("boiler", "START")

    async def ignite(ctx: Ctx) -> None:
        await ctx.write("boiler", "MANUAL_OUTPUT", 15.0)

    async def pressurise(ctx: Ctx) -> None:
        ctx.boiler_pressure.to_auto(ctx.pv("boiler", "BURNER_OUTPUT"))

    async def open_valve(ctx: Ctx) -> None:
        await ctx.pulse("steam_valve", "START")
        await ctx.pulse("turbine", "START")
        ctx.turbine_speed.to_auto(0.0)

    async def excite_generator(ctx: Ctx) -> None:
        # 同步檢查之前必須先勵磁：AVR 只有在發電機 RUNNING 時才建立電壓，
        # 電壓為 0 時 SYNC_PERMISSIVE 的「電壓在範圍內」永遠不成立，
        # SYNC_CHECK 會一路等到逾時。
        await ctx.pulse("generator", "START")

    async def close_breaker(ctx: Ctx) -> None:
        await ctx.pulse("generator", "START")
        await ctx.pulse("generator", "BREAKER_CLOSE")

    async def ramp_load(ctx: Ctx) -> None:
        await ctx.write("generator", "PRIMARY_SETPOINT", ctx.target_load_mw)

    return [
        Step("COOLING_WATER", "啟動冷凝器冷卻系統", start_cooling,
             lambda c: c.pv("condenser", "COOLING_WATER_AVAILABILITY") > 90.0, timeout=120),
        Step("PULL_VACUUM", "建立冷凝器真空", None,
             lambda c: c.pv("condenser", "CONDENSER_PRESSURE") <= 0.12, timeout=300),
        Step("CHECK_HOTWELL", "確認熱井水位", None,
             lambda c: c.pv("condenser", "HOTWELL_LEVEL") >= 20.0, timeout=120),
        Step("START_CONDENSATE_PUMP", "啟動凝結水泵", start_condensate_pump,
             lambda c: c.di("condensate_pump", "RUNNING"),
             guard=lambda c: (c.pv("condenser", "HOTWELL_LEVEL") >= 20.0, "熱井低低水位禁止啟動凝結水泵"),
             timeout=120),
        Step("TANK_LEVEL", "將給水槽水位控制至 60%", tank_to_setpoint,
             lambda c: abs(c.pv("feedwater_tank", "TANK_LEVEL") - 60.0) < 5.0, timeout=600),
        Step("START_FEEDWATER_PUMP", "啟動給水泵", start_feedwater_pump,
             lambda c: c.di("feedwater_pump", "RUNNING"),
             guard=lambda c: (c.pv("feedwater_tank", "TANK_LEVEL") >= 25.0,
                              "給水槽低低水位禁止啟動給水泵"),
             timeout=120),
        Step("BOILER_LEVEL", "將鍋爐水位控制至 66.7%", boiler_level_auto,
             lambda c: abs(c.pv("boiler", "LEVEL_INDICATED") - 66.7) < 5.0, timeout=900),
        Step("BOILER_PURGE", "執行鍋爐吹掃", purge,
             lambda c: c.pv("boiler", "DEVICE_STATE") in (6, 7, 2),
             guard=lambda c: (30.0 <= c.pv("boiler", "LEVEL_INDICATED") <= 80.0,
                              "鍋爐水位不安全，禁止點火"),
             timeout=180),
        Step("IGNITE", "點火並緩慢升壓", ignite,
             lambda c: c.pv("boiler", "FLAME_STATUS") >= 2, timeout=120),
        Step("RAISE_PRESSURE", "鍋爐壓力超過最低啟動壓力", pressurise,
             lambda c: c.pv("boiler", "BOILER_PRESSURE") >= c.min_turbine_pressure, timeout=1800),
        Step("OPEN_MSV", "緩慢開啟主蒸汽閥並升速", open_valve,
             lambda c: c.pv("turbine", "SPEED_RPM") > 300.0,
             guard=lambda c: (c.pv("condenser", "CONDENSER_PRESSURE") <= 0.15,
                              "冷凝器真空不良，禁止啟動汽輪機"),
             timeout=300),
        Step("RUN_UP", "汽輪機達 3000 RPM", None,
             lambda c: abs(c.pv("turbine", "SPEED_RPM") - 3000.0) <= 30.0, timeout=900),
        Step("SYNC_CHECK", "發電機勵磁與同步檢查", excite_generator,
             lambda c: c.pv("generator", "SYNC_PERMISSIVE") == 0x3F, timeout=300),
        Step("CLOSE_BREAKER", "閉合發電機斷路器", close_breaker,
             lambda c: c.pv("generator", "BREAKER_STATUS") >= 1,
             guard=lambda c: (
                 abs(c.pv("turbine", "SPEED_RPM") - 3000.0) <= 30.0
                 and c.pv("generator", "SYNC_PERMISSIVE") == 0x3F,
                 "汽輪機轉速不符或同步條件不成立，禁止閉合斷路器",
             ),
             # 相角差會持續滑移，同步窗口是一閃即逝的：必須反覆嘗試，
             # 直到窗口再次打開為止，不能只在進入步驟時試一次
             repeat_enter=True,
             timeout=180),
        Step("RAMP_LOAD", "逐步增加負載", ramp_load,
             lambda c: c.pv("generator", "ELECTRICAL_POWER") >= c.target_load_mw * 0.95,
             # 併聯後先讓鍋爐壓力回到設定值附近再加載：
             # 對著軟掉的鍋爐加載會讓壓力與轉速一起垮
             guard=lambda c: (
                 c.pv("boiler", "BOILER_PRESSURE") >= c.boiler_pressure.setpoint * 0.9,
                 "鍋爐壓力不足，暫緩加載",
             ),
             timeout=1800),
        Step("NORMAL", "進入正常自動控制", None, lambda c: True, timeout=60),
    ]


@dataclass
class Sequencer:
    steps: list[Step] = field(default_factory=build_sequence)
    index: int = -1
    elapsed: float = 0.0
    running: bool = False
    finished: bool = False
    last_error: str = ""
    entered: bool = False

    @property
    def current(self) -> Step | None:
        if 0 <= self.index < len(self.steps):
            return self.steps[self.index]
        return None

    def start(self) -> None:
        self.index = 0
        self.elapsed = 0.0
        self.running = True
        self.finished = False
        self.entered = False
        self.last_error = ""

    def stop(self) -> None:
        self.running = False

    async def update(self, ctx: Ctx, dt: float) -> None:
        if not self.running or self.finished:
            return
        step = self.current
        if step is None:
            self.finished = True
            self.running = False
            return
        if not self.entered:
            if step.guard:
                allowed, reason = step.guard(ctx)
                if not allowed:
                    if self.last_error != reason:
                        self.last_error = reason
                        ctx.emit("SEQUENCE_BLOCKED", step=step.name, reason=reason)
                    return
            self.last_error = ""
            if step.enter:
                await step.enter(ctx)
            ctx.emit("SEQUENCE_STEP_ENTER", step=step.name, description=step.description,
                     index=self.index)
            self.entered = True
            self.elapsed = 0.0
        elif step.repeat_enter and step.enter and not step.done(ctx):
            # 重試型步驟：guard 成立時才重下命令，避免對著不成立的允許條件
            # 反覆送出必被拒絕的命令而灌爆事件記錄
            allowed = True
            if step.guard:
                allowed, reason = step.guard(ctx)
                if not allowed and self.last_error != reason:
                    self.last_error = reason
                    ctx.emit("SEQUENCE_BLOCKED", step=step.name, reason=reason)
            if allowed:
                self.last_error = ""
                await step.enter(ctx)
        self.elapsed += dt
        if step.done(ctx):
            ctx.emit("SEQUENCE_STEP_DONE", step=step.name, elapsed=round(self.elapsed, 1))
            self.index += 1
            self.entered = False
            if self.index >= len(self.steps):
                self.finished = True
                self.running = False
                ctx.emit("SEQUENCE_COMPLETE")
        elif self.elapsed > step.timeout:
            ctx.emit("SEQUENCE_STEP_TIMEOUT", step=step.name, elapsed=round(self.elapsed, 1))
            self.running = False
            self.last_error = f"{step.name} 逾時"

    def to_dict(self) -> dict:
        return {"index": self.index, "elapsed": self.elapsed, "running": self.running,
                "finished": self.finished, "entered": self.entered, "last_error": self.last_error}

    def from_dict(self, data: dict) -> None:
        self.index = int(data.get("index", -1))
        self.elapsed = float(data.get("elapsed", 0.0))
        self.running = bool(data.get("running", False))
        self.finished = bool(data.get("finished", False))
        self.entered = bool(data.get("entered", False))
        self.last_error = str(data.get("last_error", ""))
