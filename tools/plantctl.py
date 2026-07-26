"""plantctl — 模擬器控制與快速狀態恢復 CLI。

不需要重啟 docker 即可把整套環境回到指定快照：

    plantctl snapshot save steady-60mw -d "60 MW 穩態基準"
    plantctl snapshot list
    plantctl snapshot restore steady-60mw          # 忠實還原（含跳機鎖存）
    plantctl snapshot restore steady-60mw --clean  # 還原後清除鎖存，作為乾淨測試起點
    plantctl rollback                              # 還原最後一次快照

其他：
    plantctl status | watch | pause | resume | step 10 | speed 5
    plantctl fault set --target condenser --category process \\
             --name cooling_water_availability --value 0.3
    plantctl write --device generator --register PRIMARY_SETPOINT --value 90
    plantctl scenario run scenarios/load_step.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BUS_API = os.environ.get("BUS_API", "http://127.0.0.1:15080")
MODBUS_HOST = os.environ.get("MODBUS_HOST", "127.0.0.1")

# 設備 -> 主機埠（compose 預設）
DEVICE_PORTS = {
    "dcs-plc": 15020,
    "condenser": 15021,
    "condensate_pump": 15022,
    "feedwater_tank": 15023,
    "feedwater_pump": 15024,
    "boiler": 15025,
    "steam_valve": 15026,
    "turbine": 15027,
    "generator": 15028,
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 20.0) -> dict:
    url = BUS_API.rstrip("/") + path
    data = json.dumps(body or {}).encode() if method in ("POST", "PUT", "DELETE") else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"error": payload, "status": exc.code}
    except urllib.error.URLError as exc:
        return {"error": f"無法連線 {url}: {exc.reason}"}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {"raw": payload}


def show(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------
# Modbus（情境與手動寫入用）
# --------------------------------------------------------------------------
def _register_map(device: str):
    from common.modbus.register_map import RegisterMap
    from controller.dcs.main import DEVICE_CLASSES

    klass = DEVICE_CLASSES[device]
    return RegisterMap.build(device, klass.PROCESS_INPUTS, klass.EXTRA_HOLDINGS, klass.EXTRA_COILS)


def modbus_write(device: str, register: str, value: float, coil: bool = False) -> dict:
    from pymodbus.client import ModbusTcpClient

    from common.modbus.register_map import Table

    rmap = _register_map(device)
    port = DEVICE_PORTS[device]
    client = ModbusTcpClient(MODBUS_HOST, port=port, timeout=3)
    try:
        if not client.connect():
            return {"error": f"無法連線 {MODBUS_HOST}:{port}"}
        if coil:
            spec = rmap.by_name(Table.COIL, register)
            result = client.write_coil(spec.offset, bool(value), device_id=1)
        else:
            spec = rmap.by_name(Table.HOLDING, register)
            raw = max(0, min(0xFFFF, int(round(value * spec.scale))))
            result = client.write_register(spec.offset, raw, device_id=1)
        return {"device": device, "register": register, "value": value,
                "offset": spec.offset, "ok": not result.isError(), "response": str(result)}
    finally:
        client.close()


def modbus_read(device: str, register: str) -> dict:
    from pymodbus.client import ModbusTcpClient

    from common.modbus.register_map import Table

    rmap = _register_map(device)
    client = ModbusTcpClient(MODBUS_HOST, port=DEVICE_PORTS[device], timeout=3)
    try:
        if not client.connect():
            return {"error": "connect failed"}
        spec = rmap.by_name(Table.INPUT, register)
        count = 2 if spec.dtype == "u32" else 1
        result = client.read_input_registers(spec.offset, count=count, device_id=1)
        if result.isError():
            return {"error": str(result)}
        if count == 2:
            raw = (result.registers[0] << 16) | result.registers[1]
        else:
            raw = result.registers[0]
            if spec.dtype == "i16" and raw >= 0x8000:
                raw -= 0x10000
        return {"device": device, "register": register, "value": raw / spec.scale,
                "raw": result.registers}
    finally:
        client.close()


# --------------------------------------------------------------------------
# 指令
# --------------------------------------------------------------------------
def cmd_status(args) -> int:
    state = api("/state")
    if "error" in state:
        show(state)
        return 1
    print(f"sim_time={state['sim_time']}s tick={state['tick']} "
          f"paused={state['paused']} speed={state['speed']} "
          f"snapshot_generation={state['snapshot_generation']}")
    print("\n設備：")
    for name, info in sorted(state["participants"].items()):
        flag = "TRIP" if info["tripped"] else ""
        print(f"  {name:<18} state={info['state']:<2} missed={info['missed_ticks']:<4} "
              f"quality={info['quality']:<6} {flag}")
    for name in state["offline_devices"]:
        print(f"  {name:<18} OFFLINE")
    print("\n程序量：")
    for name, sig in sorted(state["signals"].items()):
        print(f"  {name:<36} {sig['value']:>12.4f}  {sig['quality']}")
    return 0


def cmd_watch(args) -> int:
    keys = args.signals or [
        "boiler.pressure_bar_abs", "boiler.level_pct", "turbine.speed_rpm",
        "generator.electrical_power_mw", "condenser.pressure_bar_abs",
        "feedwater_tank.level_pct", "steam_valve.position_pct",
    ]
    header = "  ".join(f"{k.split('.')[-1][:12]:>12}" for k in keys)
    print(f"{'sim_time':>9}  {header}")
    try:
        while True:
            state = api("/state")
            if "error" in state:
                show(state)
                return 1
            values = "  ".join(
                f"{state['signals'].get(k, {}).get('value', float('nan')):>12.3f}" for k in keys
            )
            print(f"{state['sim_time']:>9.1f}  {values}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_snapshot(args) -> int:
    if args.action == "save":
        result = api("/snapshot/save", "POST",
                     {"name": args.name, "description": args.description or "",
                      "tags": args.tag or []})
    elif args.action == "restore":
        result = api("/snapshot/restore", "POST",
                     {"name": args.name, "clear_latches": args.clean,
                      "keep_faults": args.keep_faults,
                      "preserve_totalizers": args.preserve_totalizers,
                      "resume": not args.stay_paused})
    elif args.action == "list":
        result = api("/snapshot")
    elif args.action == "show":
        result = api(f"/snapshot/{args.name}" + ("?full=1" if args.full else ""))
    elif args.action == "delete":
        result = api(f"/snapshot/{args.name}", "DELETE")
    else:
        return 2
    show(result)
    return 0 if "error" not in result else 1


def cmd_rollback(args) -> int:
    listing = api("/snapshot")
    name = args.name or listing.get("last")
    if not name:
        snapshots = listing.get("snapshots") or []
        if not snapshots:
            print("沒有可用快照")
            return 1
        name = snapshots[0]["name"]
    show(api("/snapshot/restore", "POST", {"name": name, "clear_latches": args.clean,
                                           "resume": True}))
    return 0


def cmd_sim(args) -> int:
    if args.action == "pause":
        show(api("/sim/pause", "POST"))
    elif args.action == "resume":
        show(api("/sim/resume", "POST"))
    elif args.action == "step":
        show(api("/sim/step", "POST", {"ticks": args.ticks}))
    elif args.action == "speed":
        show(api("/sim/speed", "POST", {"speed": args.value}))
    return 0


def cmd_fault(args) -> int:
    if args.action == "clear":
        show(api("/fault/clear", "POST", {"target": args.target, "category": args.category,
                                          "name": args.name}))
        return 0
    spec: object
    if args.category == "sensor":
        spec = {"mode": args.mode, "value": args.value, "bias": args.bias,
                "noise": args.noise, "drift": args.drift}
    elif args.category == "comm":
        spec = json.loads(args.spec) if args.spec else {}
    elif args.category == "actuator":
        spec = args.spec if args.spec is not None else args.mode
    else:
        spec = args.value
    show(api("/fault", "POST", {"target": args.target, "category": args.category,
                                "name": args.name, "spec": spec}))
    return 0


def cmd_events(args) -> int:
    query = f"/events?limit={args.limit}"
    if args.device:
        query += f"&device={args.device}"
    if args.event:
        query += f"&event={args.event}"
    for record in api(query):
        line = (f"{record.get('sim_time', 0):>9} {record.get('device', ''):<16} "
                f"{record.get('event', ''):<26}")
        extras = {k: v for k, v in record.items()
                  if k not in ("sim_time", "wall_time", "device", "event")}
        print(line + (json.dumps(extras, ensure_ascii=False) if extras else ""))
    return 0


def cmd_write(args) -> int:
    show(modbus_write(args.device, args.register, args.value, coil=args.coil))
    return 0


def cmd_read(args) -> int:
    show(modbus_read(args.device, args.register))
    return 0


def cmd_signal(args) -> int:
    show(api("/signal/force", "POST",
             {"name": args.name, "value": None if args.release else args.value}))
    return 0


def cmd_scenario(args) -> int:
    from tools.scenario_runner import run_scenario

    return run_scenario(args.file, api, modbus_write, verbose=not args.quiet)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plantctl", description="火力發電廠模擬器控制工具")
    parser.add_argument("--api", default=BUS_API, help="plant-bus 管理 API")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="顯示全廠狀態").set_defaults(func=cmd_status)

    watch = sub.add_parser("watch", help="持續顯示重點程序量")
    watch.add_argument("signals", nargs="*")
    watch.add_argument("--interval", type=float, default=1.0)
    watch.set_defaults(func=cmd_watch)

    snap = sub.add_parser("snapshot", help="快照：save/restore/list/show/delete")
    snap.add_argument("action", choices=["save", "restore", "list", "show", "delete"])
    snap.add_argument("name", nargs="?", default="manual")
    snap.add_argument("-d", "--description", default="")
    snap.add_argument("-t", "--tag", action="append")
    snap.add_argument("--clean", action="store_true", help="還原後清除跳機鎖存與警報")
    snap.add_argument("--keep-faults", action="store_true", help="還原時保留目前注入的故障")
    snap.add_argument("--preserve-totalizers", action="store_true", help="保留目前累積量")
    snap.add_argument("--stay-paused", action="store_true", help="還原後維持暫停")
    snap.add_argument("--full", action="store_true")
    snap.set_defaults(func=cmd_snapshot)

    rollback = sub.add_parser("rollback", help="還原最後一次快照")
    rollback.add_argument("name", nargs="?")
    rollback.add_argument("--clean", action="store_true")
    rollback.set_defaults(func=cmd_rollback)

    for action in ("pause", "resume"):
        item = sub.add_parser(action, help=f"{action} 模擬")
        item.set_defaults(func=cmd_sim, action=action)
    step = sub.add_parser("step", help="單步執行 N 個 tick")
    step.add_argument("ticks", type=int, nargs="?", default=1)
    step.set_defaults(func=cmd_sim, action="step")
    speed = sub.add_parser("speed", help="設定模擬速度倍率")
    speed.add_argument("value", type=float)
    speed.set_defaults(func=cmd_sim, action="speed")

    fault = sub.add_parser("fault", help="故障注入（需 LAB_MODE=true）")
    fault.add_argument("action", choices=["set", "clear"])
    fault.add_argument("--target", default="*")
    fault.add_argument("--category", choices=["sensor", "actuator", "process", "comm"],
                       default="process")
    fault.add_argument("--name", default=None)
    fault.add_argument("--mode", default="stuck_at")
    fault.add_argument("--value", type=float, default=0.0)
    fault.add_argument("--bias", type=float, default=0.0)
    fault.add_argument("--noise", type=float, default=0.0)
    fault.add_argument("--drift", type=float, default=0.0)
    fault.add_argument("--spec", default=None, help="actuator/comm 用的原始值或 JSON")
    fault.set_defaults(func=cmd_fault)

    events = sub.add_parser("events", help="顯示事件")
    events.add_argument("--limit", type=int, default=50)
    events.add_argument("--device")
    events.add_argument("--event")
    events.set_defaults(func=cmd_events)

    write = sub.add_parser("write", help="以 Modbus 寫入 Holding Register 或 Coil")
    write.add_argument("--device", required=True, choices=sorted(DEVICE_PORTS))
    write.add_argument("--register", required=True)
    write.add_argument("--value", type=float, required=True)
    write.add_argument("--coil", action="store_true")
    write.set_defaults(func=cmd_write)

    read = sub.add_parser("read", help="以 Modbus 讀取 Input Register")
    read.add_argument("--device", required=True, choices=sorted(DEVICE_PORTS))
    read.add_argument("--register", required=True)
    read.set_defaults(func=cmd_read)

    signal = sub.add_parser("signal", help="強制/釋放模擬匯流排訊號")
    signal.add_argument("name")
    signal.add_argument("value", type=float, nargs="?", default=0.0)
    signal.add_argument("--release", action="store_true")
    signal.set_defaults(func=cmd_signal)

    scenario = sub.add_parser("scenario", help="執行情境檔")
    scenario.add_argument("action", choices=["run"])
    scenario.add_argument("file")
    scenario.add_argument("--quiet", action="store_true")
    scenario.set_defaults(func=cmd_scenario)

    return parser


def main() -> int:
    global BUS_API
    parser = build_parser()
    args = parser.parse_args()
    BUS_API = args.api
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
