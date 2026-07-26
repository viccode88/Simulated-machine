"""模糊測試 harness。

每一輪：
  1. 還原相同的 plant snapshot（不重啟容器，毫秒級）
  2. 對目標送出一批 fuzz frames，記錄 request/response
  3. 檢查設備是否仍存活、Modbus 是否仍符合規格
  4. 檢查物理安全不變量
  5. 失敗時輸出 crash artifact（最後封包、事件、狀態）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import struct
import time
import urllib.error
import urllib.request
from collections import deque

from tools.fuzz.fuzzer import next_frame
from tools.invariants import InvariantChecker

BUS_API = os.environ.get("BUS_API", "http://plant-bus:8080")
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "/var/lib/crash")


def api(path: str, method: str = "GET", body: dict | None = None, timeout: float = 20.0) -> dict:
    url = BUS_API.rstrip("/") + path
    data = json.dumps(body or {}).encode() if method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": exc.read().decode(), "status": exc.code}
    except Exception as exc:
        return {"error": repr(exc)}


class Target:
    def __init__(self, spec: str) -> None:
        host, _, port = spec.partition(":")
        self.host = host
        self.port = int(port or 502)
        self.name = host
        self.sock: socket.socket | None = None
        self.sent = 0
        self.responses = 0
        self.exceptions = 0
        self.timeouts = 0
        self.disconnects = 0
        self.history: deque[dict] = deque(maxlen=50)

    def connect(self) -> bool:
        self.close()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=2.0)
            self.sock.settimeout(2.0)
            return True
        except Exception:
            self.sock = None
            return False

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def send(self, kind: str, frame: bytes) -> dict:
        record = {"kind": kind, "request": frame.hex(), "ts": time.time()}
        if self.sock is None and not self.connect():
            record["result"] = "CONNECT_FAILED"
            self.history.append(record)
            return record
        try:
            self.sock.sendall(frame)  # type: ignore[union-attr]
            self.sent += 1
            header = self.sock.recv(7)  # type: ignore[union-attr]
            if not header:
                record["result"] = "CLOSED_BY_PEER"
                self.disconnects += 1
                self.close()
            elif len(header) < 7:
                record["result"] = "SHORT_HEADER"
                self.disconnects += 1
                self.close()
            else:
                _, _, length, _ = struct.unpack(">HHHB", header)
                body = self.sock.recv(max(0, length - 1)) if length > 1 else b""  # type: ignore
                record["response"] = (header + body).hex()
                self.responses += 1
                if body and body[0] & 0x80:
                    record["result"] = f"EXCEPTION_{body[1]:02X}"
                    self.exceptions += 1
                else:
                    record["result"] = "OK"
        except socket.timeout:
            record["result"] = "TIMEOUT"
            self.timeouts += 1
            self.close()
        except Exception as exc:
            record["result"] = f"ERROR:{exc!r}"
            self.disconnects += 1
            self.close()
        self.history.append(record)
        return record

    def alive(self) -> bool:
        """以一次合法讀取確認設備仍正常回應（Register Map Version）。"""
        self.close()
        if not self.connect():
            return False
        pdu = struct.pack(">BHH", 0x04, 7, 1)
        record = self.send("liveness", struct.pack(">HHHB", 0xFFFF, 0, len(pdu) + 1, 1) + pdu)
        return record.get("result") == "OK"

    def stats(self) -> dict:
        return {"target": f"{self.host}:{self.port}", "sent": self.sent,
                "responses": self.responses, "exceptions": self.exceptions,
                "timeouts": self.timeouts, "disconnects": self.disconnects}


def save_artifact(name: str, payload: dict) -> str:
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    path = os.path.join(ARTIFACT_DIR, f"{name}-{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    print(f"[harness] crash artifact -> {path}", flush=True)
    return path


def ensure_baseline(name: str) -> bool:
    listing = api("/snapshot")
    names = {m.get("name") for m in listing.get("snapshots", [])}
    if name in names:
        return True
    print(f"[harness] 建立基準快照 {name}", flush=True)
    meta = api("/snapshot/save", "POST", {"name": name, "description": "fuzz 基準",
                                          "tags": ["fuzz", "baseline"]})
    return "error" not in meta


def run(args: argparse.Namespace) -> int:
    random.seed(args.seed)
    targets = [Target(spec.strip()) for spec in args.targets.split(",") if spec.strip()]
    checker = InvariantChecker()
    print(f"[harness] targets={[t.stats()['target'] for t in targets]}", flush=True)

    for _ in range(60):
        if "error" not in api("/health"):
            break
        time.sleep(2)

    if not ensure_baseline(args.baseline):
        print("[harness] 無法建立基準快照", flush=True)
        return 2

    deadline = time.time() + args.duration
    round_index = 0
    failures = 0
    transaction = 0
    interval = 1.0 / max(1.0, args.rate_limit)

    while time.time() < deadline:
        round_index += 1
        restore = api("/snapshot/restore", "POST",
                      {"name": args.baseline, "clear_latches": True, "resume": True})
        if "error" in restore:
            print(f"[harness] 還原失敗：{restore['error']}", flush=True)
            return 2
        checker.reset_baseline()
        print(f"[harness] round {round_index} 還原耗時 {restore.get('elapsed_ms')} ms",
              flush=True)

        for _ in range(args.frames_per_round):
            transaction = (transaction + 1) & 0xFFFF
            target = random.choice(targets)
            kind, frame = next_frame(transaction)
            target.send(kind, frame)
            time.sleep(interval)

        # --- 檢查存活與規格一致性 ---
        for target in targets:
            if not target.alive():
                failures += 1
                save_artifact(f"dead-{target.name}", {
                    "target": target.stats(),
                    "last_frames": list(target.history),
                    "bus_state": api("/state"),
                    "events": api("/events?limit=200"),
                    "round": round_index,
                })
        state = api("/state")
        violations = checker.update(state)
        if violations:
            failures += 1
            save_artifact("invariant", {"violations": violations, "bus_state": state,
                                        "round": round_index})
        offline = state.get("offline_devices") or []
        if offline:
            failures += 1
            save_artifact("offline", {"offline": offline, "bus_state": state,
                                      "events": api("/events?limit=200")})

        print(f"[harness] round {round_index} 完成 stats="
              f"{json.dumps([t.stats() for t in targets], ensure_ascii=False)} "
              f"failures={failures}", flush=True)

    summary = {
        "rounds": round_index,
        "failures": failures,
        "targets": [t.stats() for t in targets],
        "invariants": checker.summary(),
    }
    print("[harness] 完成 " + json.dumps(summary, ensure_ascii=False), flush=True)
    save_artifact("summary", summary)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Modbus 模糊測試 harness")
    parser.add_argument("--targets", default=os.environ.get(
        "TARGETS", "boiler:502,turbine:502,steam-valve:502,generator:502"))
    parser.add_argument("--duration", type=float, default=float(os.environ.get("DURATION", 300)))
    parser.add_argument("--frames-per-round", type=int, default=int(
        os.environ.get("FRAMES_PER_ROUND", 400)))
    parser.add_argument("--rate-limit", type=float, default=float(os.environ.get("RATE_LIMIT", 200)))
    parser.add_argument("--baseline", default=os.environ.get("BASELINE_SNAPSHOT", "fuzz-baseline"))
    parser.add_argument("--seed", type=int, default=int(os.environ.get("SEED", 1337)))
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
