#!/usr/bin/env python3
"""外部 PLC 骨架 —— 示範如何以 Modbus TCP 串接 8 台設備。

刻意**不 import 專案內的設備類別**，介面契約只來自 docs/register-map.csv，
所以這支程式跟真正的第三方 PLC 處於同樣的資訊條件。

    docker compose --profile external-plc up -d      # 不啟動內建 DCS
    python examples/external_plc.py --host 127.0.0.1

    python examples/external_plc.py --selftest        # 離線自我檢查（不需設備）
    python examples/external_plc.py --read-only       # 只監看，不搶寫入控制權
    python examples/external_plc.py --only boiler     # 只接一台

詳細說明見 docs/plc-integration.md。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# 常數：全部來自 configs/plant.yaml 與 docs/register-map.csv，不要在別處硬編碼
# --------------------------------------------------------------------------
UNIT_ID = 1
RESET_KEY_VALUE = 0xA55A          # 42330
WATCHDOG_PERIOD_S = 1.0           # 必須 < comm.watchdog_timeout (3.0)
POLL_PERIOD_S = 0.25
CONTROL_PERIOD_S = 0.5
OVERSPEED_CLOSE_RPM = 3150.0      # 比設備跳機門檻 3300 早動作

# 主機模式的埠對映（compose.yaml）；容器模式一律用 502
HOST_PORTS = {
    "condenser": 15021, "condensate_pump": 15022, "feedwater_tank": 15023,
    "feedwater_pump": 15024, "boiler": 15025, "steam_valve": 15026,
    "turbine": 15027, "generator": 15028,
}
CONTAINER_HOSTS = {name: name.replace("_", "-") for name in HOST_PORTS}

# 跳機矩陣：來源設備 TRIPPED 上升緣 -> 要執行的動作
TRIP_MATRIX = {
    "turbine": [("generator", "coil", "BREAKER_OPEN", 1),
                ("steam_valve", "hr", "MANUAL_OUTPUT", 0.0),
                ("generator", "hr", "PRIMARY_SETPOINT", 0.0)],
    "boiler": [("boiler", "hr", "MANUAL_OUTPUT", 0.0),
               ("steam_valve", "hr", "MANUAL_OUTPUT", 0.0)],
    "condenser": [("generator", "hr", "PRIMARY_SETPOINT", 0.0)],
    "feedwater_pump": [("boiler", "hr", "MANUAL_OUTPUT", 0.0)],
    "condensate_pump": [("feedwater_pump", "hr", "MANUAL_OUTPUT", 20.0)],
    "steam_valve": [("boiler", "hr", "MANUAL_OUTPUT", 0.0)],
}


# --------------------------------------------------------------------------
# 1. 介面契約：載入 docs/register-map.csv
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    table: str        # COIL / DISCRETE / INPUT / HOLDING
    offset: int       # PDU offset —— 線上真正送出的位址
    dtype: str        # u16 / i16 / u32 / enum / bitfield
    scale: float
    lo: float | None
    hi: float | None
    writable: bool

    @property
    def words(self) -> int:
        return 2 if self.dtype == "u32" else 1


def load_register_map(path: str) -> dict[str, dict[tuple[str, str], Spec]]:
    """回傳 {device: {(table, name): Spec}}。"""
    rmap: dict[str, dict[tuple[str, str], Spec]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            def num(key: str) -> float | None:
                value = (row.get(key) or "").strip()
                return float(value) if value else None

            rmap.setdefault(row["device"], {})[(row["table"], row["name"])] = Spec(
                table=row["table"],
                offset=int(row["pdu_offset"]),
                dtype=row["dtype"],
                scale=num("scale") or 1.0,
                lo=num("min"), hi=num("max"),
                writable=row["writable"] == "yes",
            )
    return rmap


def decode(spec: Spec, words: list[int]) -> float:
    """原始暫存器 -> 工程值。u32 高 word 在前；i16 需還原負數。"""
    raw = words[0]
    if spec.dtype == "u32":
        raw = (words[0] << 16) | words[1]
    elif spec.dtype == "i16" and raw >= 0x8000:
        raw -= 0x10000
    return raw / spec.scale


def encode(spec: Spec, value: float) -> int:
    """工程值 -> 原始暫存器，並先 clamp 到 min/max，避免拿 Exception 03。"""
    if spec.lo is not None:
        value = max(spec.lo, value)
    if spec.hi is not None:
        value = min(spec.hi, value)
    raw = int(round(value * spec.scale))
    if spec.dtype == "i16" and raw < 0:
        raw += 0x10000
    return max(0, min(0xFFFF, raw))


# --------------------------------------------------------------------------
# 2. PID（含抗飽和與 bumpless transfer）
# --------------------------------------------------------------------------
@dataclass
class PID:
    kp: float
    ki: float
    setpoint: float
    out_min: float = 0.0
    out_max: float = 100.0
    integral: float = 0.0
    output: float = 0.0
    auto: bool = False

    def to_auto(self, current_output: float) -> None:
        """接手時把積分項預載成目前輸出 -> 切換瞬間不跳動（bumpless）。"""
        self.output = current_output
        self.integral = current_output
        self.auto = True

    def force_output(self, value: float) -> None:
        """安全邏輯介入時用；同時清積分，避免解除後暴衝。"""
        self.output = value
        self.integral = value

    def update(self, pv: float, dt: float, feedforward: float = 0.0) -> float:
        error = self.setpoint - pv
        candidate = self.integral + self.ki * error * dt
        output = self.kp * error + candidate + feedforward
        # 抗飽和：只有輸出沒有飽和時才累積積分
        if self.out_min < output < self.out_max:
            self.integral = candidate
        self.output = max(self.out_min, min(self.out_max, output))
        return self.output


# --------------------------------------------------------------------------
# 3. 單一設備的連線與資料快取
# --------------------------------------------------------------------------
class DeviceLink:
    def __init__(self, name: str, host: str, port: int, specs: dict, read_only: bool) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.specs = specs
        self.read_only = read_only
        # pymodbus 的 AsyncModbusTcpClient 建構時就要求已有執行中的事件迴圈，
        # 因此連線物件一律延後到 connect() 內才建立。
        self.client = None
        self.inputs = [0] * 50
        self.discretes = [False] * 16
        self.holdings = [0] * 32
        self.online = False
        self.watchdog = 0
        self.command_sequence = 0
        self.exception_counts: dict[int, int] = {}
        self.last_generation = -1

    # -- 連線 --------------------------------------------------------------
    async def connect(self) -> None:
        from pymodbus.client import AsyncModbusTcpClient

        if self.client is None:
            self.client = AsyncModbusTcpClient(self.host, port=self.port, timeout=1.0, retries=1)
        try:
            await self.client.connect()
        except Exception as exc:
            print(f"[CONNECT_FAILED] {self.name}: {exc!r}", file=sys.stderr)

    # -- 讀 ----------------------------------------------------------------
    async def poll(self) -> bool:
        """一個週期三次批次讀取。映像是不可變快照，所以不會撕裂讀取。"""
        if self.client is None:
            await self.connect()
        if self.client is None:
            return False
        try:
            ir = await self.client.read_input_registers(0, count=50, device_id=UNIT_ID)
            if ir.isError():
                self._note_exception(ir)
                raise IOError(str(ir))
            self.inputs[: len(ir.registers)] = ir.registers

            di = await self.client.read_discrete_inputs(0, count=16, device_id=UNIT_ID)
            if not di.isError():
                self.discretes = list(di.bits)[:16]

            hr = await self.client.read_holding_registers(0, count=32, device_id=UNIT_ID)
            if not hr.isError():
                self.holdings[: len(hr.registers)] = hr.registers

            self.online = True
            return True
        except Exception:
            self.online = False
            return False

    def ir(self, name: str) -> float:
        spec = self.specs[("INPUT", name)]
        return decode(spec, self.inputs[spec.offset: spec.offset + spec.words])

    def di(self, name: str) -> bool:
        return bool(self.discretes[self.specs[("DISCRETE", name)].offset])

    def hr(self, name: str) -> float:
        spec = self.specs[("HOLDING", name)]
        return decode(spec, self.holdings[spec.offset: spec.offset + spec.words])

    def has(self, table: str, name: str) -> bool:
        return (table, name) in self.specs

    # -- 寫 ----------------------------------------------------------------
    def _note_exception(self, result) -> None:
        code = int(getattr(result, "exception_code", 0) or 0)
        if code:
            self.exception_counts[code] = self.exception_counts.get(code, 0) + 1

    async def write_hr(self, name: str, value: float) -> bool:
        if self.read_only or self.client is None:
            return False
        spec = self.specs[("HOLDING", name)]
        try:
            result = await self.client.write_register(spec.offset, encode(spec, value),
                                                      device_id=UNIT_ID)
        except Exception:
            self.online = False
            return False
        if result.isError():
            self._note_exception(result)
            # 06 = 設備忙碌或控制權在別人手上 -> 等下個週期重試，不要斷線
            return False
        return True

    async def pulse_coil(self, name: str) -> bool:
        """命令是脈衝：只寫 True，設備處理完自己清掉，不要補寫 False。"""
        if self.read_only or self.client is None or not self.has("COIL", name):
            return False
        spec = self.specs[("COIL", name)]
        try:
            result = await self.client.write_coil(spec.offset, True, device_id=UNIT_ID)
        except Exception:
            self.online = False
            return False
        if result.isError():
            self._note_exception(result)
            return False
        return True

    async def kick_watchdog(self) -> None:
        """每秒一次：維持 CONTROL_WATCHDOG_OK，同時續租寫入控制權。"""
        self.watchdog = (self.watchdog + 1) % 65535 or 1
        await self.write_hr("WATCHDOG_COUNTER", self.watchdog)

    async def reset_trip(self) -> bool:
        """四要素：Reset Key + 新序號 + 脈衝 + 安全條件（最後一項由設備判斷）。"""
        self.command_sequence = (self.command_sequence + 1) % 65535 or 1
        await self.write_hr("RESET_KEY", RESET_KEY_VALUE)
        await self.write_hr("COMMAND_SEQUENCE", self.command_sequence)
        return await self.pulse_coil("RESET_TRIP")

    # -- 診斷 --------------------------------------------------------------
    def snapshot_changed(self) -> bool:
        """快照還原後 SNAPSHOT_GENERATION 會 +1 -> PLC 必須重置積分項。"""
        generation = int(self.ir("SNAPSHOT_GENERATION"))
        changed = self.last_generation >= 0 and generation != self.last_generation
        self.last_generation = generation
        return changed


# --------------------------------------------------------------------------
# 4. PLC 本體
# --------------------------------------------------------------------------
class ExternalPLC:
    def __init__(self, links: dict[str, DeviceLink], read_only: bool) -> None:
        self.links = links
        self.read_only = read_only
        self.fired: set[str] = set()
        self.boiler_pressure = PID(kp=2.0, ki=0.08, setpoint=100.0)
        self.turbine_speed = PID(kp=0.05, ki=0.005, setpoint=3000.0)
        self.started = time.monotonic()

    def link(self, name: str) -> DeviceLink | None:
        link = self.links.get(name)
        return link if link and link.online else None

    async def poll_loop(self) -> None:
        while True:
            await asyncio.gather(*[l.poll() for l in self.links.values()],
                                 return_exceptions=True)
            await asyncio.sleep(POLL_PERIOD_S)

    async def watchdog_loop(self) -> None:
        while True:
            for link in self.links.values():
                if link.online:
                    await link.kick_watchdog()
            await asyncio.sleep(WATCHDOG_PERIOD_S)

    async def control_loop(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self.control_step(CONTROL_PERIOD_S)
            except Exception as exc:
                print(f"[CONTROL_ERROR] {exc!r}", file=sys.stderr)
            await asyncio.sleep(max(0.0, CONTROL_PERIOD_S - (time.monotonic() - started)))

    async def control_step(self, dt: float) -> None:
        # --- 快照還原偵測：積分項必須跟著重置，否則還原後暴衝 ---
        for link in self.links.values():
            if link.online and link.snapshot_changed():
                print(f"[SNAPSHOT] {link.name} 已還原 -> 重置控制器狀態")
                self.boiler_pressure.auto = False
                self.turbine_speed.auto = False
                self.fired.clear()

        # --- 跳機矩陣：只在 TRIPPED 上升緣觸發一次 ---
        for source, actions in TRIP_MATRIX.items():
            link = self.link(source)
            tripped = bool(link and link.di("TRIPPED"))
            if tripped and source not in self.fired:
                self.fired.add(source)
                print(f"[TRIP_MATRIX] {source} 跳機 -> 執行 {len(actions)} 個連鎖動作")
                for device, kind, target, value in actions:
                    target_link = self.link(device)
                    if not target_link:
                        continue
                    if kind == "hr":
                        await target_link.write_hr(target, value)
                    else:
                        await target_link.pulse_coil(target)
            elif not tripped:
                self.fired.discard(source)

        # --- 迴路 1：鍋爐壓力 -> 燃燒器輸出 ---
        boiler = self.link("boiler")
        if boiler:
            if not self.boiler_pressure.auto:
                self.boiler_pressure.to_auto(boiler.ir("BURNER_OUTPUT"))
            burner = self.boiler_pressure.update(boiler.ir("BOILER_PRESSURE"), dt)
            # 安全邏輯優先於 PID
            if boiler.di("TRIPPED"):
                self.boiler_pressure.force_output(0.0)
                burner = 0.0
            await boiler.write_hr("MANUAL_OUTPUT", burner)

        # --- 迴路 2：汽輪機轉速 -> 主蒸汽閥開度 ---
        turbine, valve = self.link("turbine"), self.link("steam_valve")
        if turbine and valve:
            if not self.turbine_speed.auto:
                self.turbine_speed.to_auto(valve.ir("ACTUAL_POSITION"))
            speed = turbine.ir("SPEED_RPM")
            position = self.turbine_speed.update(speed, dt)
            # 超速無條件關閥，優先於任何 PID 輸出
            if speed > OVERSPEED_CLOSE_RPM or turbine.di("TRIPPED"):
                self.turbine_speed.force_output(0.0)
                position = 0.0
            await valve.write_hr("MANUAL_OUTPUT", position)

    async def report_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            rows = []
            for name, link in self.links.items():
                if not link.online:
                    rows.append(f"{name}=OFFLINE")
                    continue
                flags = "TRIP" if link.di("TRIPPED") else ("RUN" if link.di("RUNNING") else "off")
                rejected = int(link.ir("REJECTED_COMMAND_COUNT"))
                wd = "wd:ok" if link.di("CONTROL_WATCHDOG_OK") else "wd:LOST"
                rows.append(f"{name}={flags},{wd},rej={rejected}")
            print(f"[{time.monotonic() - self.started:7.1f}s] " + "  ".join(rows), flush=True)

    async def run(self) -> None:
        for link in self.links.values():
            await link.connect()
        mode = "READ-ONLY（不寫入、不搶控制權）" if self.read_only else "CONTROL"
        print(f"外部 PLC 啟動：{len(self.links)} 台設備，模式 {mode}", flush=True)
        await asyncio.gather(self.poll_loop(), self.watchdog_loop(),
                             self.control_loop(), self.report_loop())


# --------------------------------------------------------------------------
# 5. 離線自我檢查：驗證 CSV 解析與編解碼，不需要任何設備
# --------------------------------------------------------------------------
def selftest(rmap: dict) -> int:
    failures: list[str] = []

    def check(label: str, got, want) -> None:
        if got != want:
            failures.append(f"{label}: 得到 {got}，預期 {want}")

    check("設備數量", len(rmap), 8)

    boiler = rmap["boiler"]
    pressure = boiler[("INPUT", "BOILER_PRESSURE")]
    check("BOILER_PRESSURE offset（文件 30010 -> PDU 9）", pressure.offset, 9)
    check("BOILER_PRESSURE scale", pressure.scale, 100.0)
    check("解碼 10000 -> 100.0 bar", decode(pressure, [10000]), 100.0)

    setpoint = boiler[("HOLDING", "PRIMARY_SETPOINT")]
    check("PRIMARY_SETPOINT 可寫", setpoint.writable, True)
    check("編碼 100 bar -> 10000", encode(setpoint, 100.0), 10000)
    check("超上限自動 clamp（130 bar 上限）", encode(setpoint, 999.0), 13000)

    water = boiler[("INPUT", "WATER_MASS_HI")]
    check("u32 佔 2 words", water.words, 2)
    check("u32 高 word 在前", decode(water, [1, 0]), 65536.0)

    temp = boiler[("INPUT", "STEAM_TEMPERATURE")]
    check("i16 負數還原（-10.0 degC）", decode(temp, [0x10000 - 100]), -10.0)

    check("RESET_TRIP 是 coil offset 2", boiler[("COIL", "RESET_TRIP")].offset, 2)
    check("TRIPPED 是 discrete offset 4", boiler[("DISCRETE", "TRIPPED")].offset, 4)
    check("generator 有 BREAKER_OPEN", ("COIL", "BREAKER_OPEN") in rmap["generator"], True)

    # 通用區在 8 台設備上必須一致，PLC 才能用同一套邏輯處理
    for device, specs in rmap.items():
        check(f"{device} WATCHDOG_COUNTER offset", specs[("HOLDING", "WATCHDOG_COUNTER")].offset, 2)
        check(f"{device} SNAPSHOT_GENERATION offset",
              specs[("INPUT", "SNAPSHOT_GENERATION")].offset, 38)

    # PID 行為
    pid = PID(kp=1.0, ki=0.5, setpoint=100.0)
    pid.to_auto(42.0)
    check("bumpless：切 auto 後輸出不跳動", round(pid.output, 6), 42.0)
    pid.update(100.0, 0.5)   # 無誤差
    check("無誤差時輸出維持", round(pid.output, 6), 42.0)
    pid.force_output(0.0)
    check("force_output 同時清積分", (pid.output, pid.integral), (0.0, 0.0))

    for line in failures:
        print(f"  [FAIL] {line}")
    print(f"自我檢查：{'PASS' if not failures else f'FAIL（{len(failures)} 項）'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------
def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="外部 PLC 骨架")
    parser.add_argument("--host", default="127.0.0.1", help="主機模式的位址")
    parser.add_argument("--in-container", action="store_true",
                        help="在 control_net 內執行：用服務名 + 502 埠")
    parser.add_argument("--map", default=os.path.join(root, "docs", "register-map.csv"))
    parser.add_argument("--only", action="append", help="只連指定設備，可重複")
    parser.add_argument("--read-only", action="store_true", help="只監看，不寫入")
    parser.add_argument("--selftest", action="store_true", help="離線自我檢查後結束")
    args = parser.parse_args()

    rmap = load_register_map(args.map)
    if args.selftest:
        return selftest(rmap)

    names = args.only or list(HOST_PORTS)
    unknown = [n for n in names if n not in HOST_PORTS]
    if unknown:
        parser.error(f"未知設備 {unknown}，可用：{sorted(HOST_PORTS)}")

    links = {}
    for name in names:
        host = CONTAINER_HOSTS[name] if args.in_container else args.host
        port = 502 if args.in_container else HOST_PORTS[name]
        links[name] = DeviceLink(name, host, port, rmap[name], args.read_only)

    plc = ExternalPLC(links, args.read_only)
    try:
        asyncio.run(plc.run())
    except KeyboardInterrupt:
        print("\n停止。設備會在 watchdog 逾時後各自進入失效安全狀態。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
