"""情境執行器：讀取 scenarios/*.yaml 並驗證預期反應。

每個情境都可以先還原同一個 plant snapshot，因此測試之間互相獨立，
而且不需要重新啟動 docker。
"""
from __future__ import annotations

import time
from typing import Any, Callable

import yaml

from tools.invariants import InvariantChecker


class ScenarioResult:
    def __init__(self, name: str) -> None:
        self.name = name
        self.checks: list[tuple[str, bool, str]] = []
        self.started = time.time()

    def add(self, description: str, passed: bool, detail: str = "") -> None:
        self.checks.append((description, passed, detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def report(self) -> str:
        lines = [f"=== 情境 {self.name} ==="]
        for description, ok, detail in self.checks:
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {description}"
                         + (f" — {detail}" if detail else ""))
        lines.append(f"  結果：{'PASS' if self.passed else 'FAIL'}"
                     f"（{time.time() - self.started:.1f}s）")
        return "\n".join(lines)


def _state(api: Callable) -> dict:
    return api("/state")


def _signal(state: dict, name: str) -> float | None:
    signal = (state.get("signals") or {}).get(name)
    return None if signal is None else float(signal["value"])


def run_scenario(path: str, api: Callable, modbus_write: Callable, verbose: bool = True) -> int:
    with open(path, "r", encoding="utf-8") as handle:
        scenario = yaml.safe_load(handle) or {}
    result = ScenarioResult(scenario.get("name", path))
    checker = InvariantChecker()

    def log(message: str) -> None:
        if verbose:
            print(message, flush=True)

    setup = scenario.get("snapshot") or {}
    if setup.get("restore"):
        response = api("/snapshot/restore", "POST",
                       {"name": setup["restore"], "clear_latches": bool(setup.get("clean", True)),
                        "resume": True})
        ok = "error" not in response
        result.add(f"還原基準快照 {setup['restore']}", ok, response.get("error", ""))
        log(f"→ 還原快照 {setup['restore']}：{'OK' if ok else response.get('error')}")
        if not ok:
            print(result.report())
            return 1
    if scenario.get("speed"):
        api("/sim/speed", "POST", {"speed": float(scenario["speed"])})

    for index, step in enumerate(scenario.get("steps") or []):
        kind = next(iter(step))
        value = step[kind]
        log(f"[{index:02d}] {kind}: {value}")

        if kind == "wait":
            deadline = time.time() + float(value)
            while time.time() < deadline:
                checker.update(_state(api))
                time.sleep(0.5)

        elif kind == "write":
            response = modbus_write(value["device"], value["register"], float(value["value"]),
                                    coil=bool(value.get("coil", False)))
            result.add(f"寫入 {value['device']}.{value['register']}={value['value']}",
                       bool(response.get("ok")), str(response.get("response", response)))

        elif kind == "fault":
            response = api("/fault", "POST", {
                "target": value.get("target", "*"), "category": value.get("category", "process"),
                "name": value.get("name"), "spec": value.get("spec"),
            })
            result.add(f"注入故障 {value.get('target')}/{value.get('name')}",
                       bool(response.get("acked")), str(response))

        elif kind == "fault_clear":
            api("/fault/clear", "POST", {"target": value.get("target", "*"),
                                         "category": value.get("category"),
                                         "name": value.get("name")})

        elif kind == "signal":
            api("/signal/force", "POST", {"name": value["name"], "value": value.get("value")})

        elif kind == "snapshot_save":
            api("/snapshot/save", "POST", {"name": value["name"],
                                           "description": value.get("description", "")})

        elif kind == "snapshot_restore":
            api("/snapshot/restore", "POST", {"name": value["name"],
                                              "clear_latches": bool(value.get("clean", False))})

        elif kind == "expect":
            ok, detail = _expect_signal(api, checker, value)
            result.add(f"{value['signal']} 在 [{value.get('min')}, {value.get('max')}]", ok, detail)

        elif kind == "expect_event":
            ok, detail = _expect_event(api, value)
            result.add(f"事件 {value.get('device', '*')}/{value['event']}", ok, detail)

        elif kind == "expect_tripped":
            ok, detail = _expect_tripped(api, value)
            result.add(f"{value['device']} 跳機鎖存 = {value.get('tripped', True)}", ok, detail)

        elif kind == "check_invariants":
            violations = checker.violations
            result.add("物理安全不變量", not violations, "; ".join(violations[:5]))

        elif kind in ("pause", "resume"):
            api(f"/sim/{kind}", "POST")

        else:
            result.add(f"未知步驟 {kind}", False, "")

    violations = checker.violations
    if violations:
        result.add("物理安全不變量（全程）", False, "; ".join(violations[:5]))

    print(result.report())
    return 0 if result.passed else 1


def _expect_signal(api: Callable, checker: InvariantChecker, spec: dict) -> tuple[bool, str]:
    within = float(spec.get("within", 30.0))
    low = spec.get("min", float("-inf"))
    high = spec.get("max", float("inf"))
    hold = float(spec.get("hold", 0.0))
    deadline = time.time() + within
    ok_since: float | None = None
    last: Any = None
    while time.time() < deadline:
        state = _state(api)
        checker.update(state)
        last = _signal(state, spec["signal"])
        if last is not None and low <= last <= high:
            ok_since = ok_since or time.time()
            if time.time() - ok_since >= hold:
                return True, f"值={last:.4f}"
        else:
            ok_since = None
        time.sleep(0.5)
    return False, f"最後值={last}"


def _expect_event(api: Callable, spec: dict) -> tuple[bool, str]:
    within = float(spec.get("within", 30.0))
    deadline = time.time() + within
    while time.time() < deadline:
        query = f"/events?limit=500&event={spec['event']}"
        if spec.get("device"):
            query += f"&device={spec['device']}"
        events = api(query)
        if isinstance(events, list) and events:
            if spec.get("code") is not None:
                events = [e for e in events if e.get("code") == spec["code"]]
            if spec.get("first_out") is not None:
                events = [e for e in events if bool(e.get("first_out")) == bool(spec["first_out"])]
            if events:
                return True, f"共 {len(events)} 筆，最後 sim_time={events[-1].get('sim_time')}"
        time.sleep(0.5)
    return False, "未觀察到事件"


def _expect_tripped(api: Callable, spec: dict) -> tuple[bool, str]:
    within = float(spec.get("within", 30.0))
    expected = bool(spec.get("tripped", True))
    deadline = time.time() + within
    last = None
    while time.time() < deadline:
        state = _state(api)
        participant = (state.get("participants") or {}).get(spec["device"])
        if participant is not None:
            last = participant.get("tripped")
            if bool(last) == expected:
                return True, f"tripped={last}"
        time.sleep(0.5)
    return False, f"tripped={last}"
