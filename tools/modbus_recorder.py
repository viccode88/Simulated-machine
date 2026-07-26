"""Modbus request recorder / replayer。

錄製（在 control_net 上當透明代理）：
    python -m tools.modbus_recorder record --listen 0.0.0.0:1502 --target boiler:502 \
        --out /var/lib/crash/boiler.jsonl

回放（重現當時的封包序列，用來重製 crash）：
    python -m tools.modbus_recorder replay --target boiler:502 --file boiler.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import time


def _split(spec: str, default_port: int) -> tuple[str, int]:
    host, _, port = spec.partition(":")
    return host or "0.0.0.0", int(port or default_port)


async def _record(args: argparse.Namespace) -> int:
    listen_host, listen_port = _split(args.listen, 1502)
    target_host, target_port = _split(args.target, 502)
    sink = open(args.out, "a", encoding="utf-8") if args.out else None

    def write(record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        print(line, flush=True)
        if sink:
            sink.write(line + "\n")
            sink.flush()

    async def handle(client_reader, client_writer):
        peer = client_writer.get_extra_info("peername")
        try:
            server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
        except Exception as exc:
            client_writer.close()
            write({"event": "UPSTREAM_FAILED", "error": repr(exc)})
            return
        write({"event": "CLIENT_CONNECTED", "peer": str(peer), "ts": time.time()})
        try:
            while True:
                header = await client_reader.readexactly(7)
                txid, proto, length, unit = struct.unpack(">HHHB", header)
                body = await client_reader.readexactly(max(0, length - 1))
                started = time.perf_counter()
                server_writer.write(header + body)
                await server_writer.drain()
                response_header = await asyncio.wait_for(server_reader.readexactly(7), timeout=5)
                _, _, response_length, _ = struct.unpack(">HHHB", response_header)
                response_body = await server_reader.readexactly(max(0, response_length - 1))
                client_writer.write(response_header + response_body)
                await client_writer.drain()
                write({
                    "event": "TRANSACTION",
                    "ts": time.time(),
                    "peer": str(peer),
                    "transaction_id": txid,
                    "protocol_id": proto,
                    "unit_id": unit,
                    "function_code": body[0] if body else None,
                    "request": (header + body).hex(),
                    "response": (response_header + response_body).hex(),
                    "exception": bool(response_body and response_body[0] & 0x80),
                    "exception_code": response_body[1] if (response_body and response_body[0] & 0x80)
                    else None,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                })
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            pass
        finally:
            write({"event": "CLIENT_CLOSED", "peer": str(peer)})
            client_writer.close()
            server_writer.close()

    server = await asyncio.start_server(handle, listen_host, listen_port)
    write({"event": "RECORDER_LISTENING", "listen": f"{listen_host}:{listen_port}",
           "target": f"{target_host}:{target_port}"})
    async with server:
        await server.serve_forever()
    return 0


def _replay(args: argparse.Namespace) -> int:
    host, port = _split(args.target, 502)
    frames: list[bytes] = []
    with open(args.file, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "TRANSACTION" and record.get("request"):
                frames.append(bytes.fromhex(record["request"]))
    print(f"回放 {len(frames)} 筆封包 -> {host}:{port}")
    mismatches = 0
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(5)
        for index, frame in enumerate(frames):
            try:
                sock.sendall(frame)
                header = sock.recv(7)
                if len(header) < 7:
                    print(f"[{index}] 連線關閉")
                    mismatches += 1
                    break
                _, _, length, _ = struct.unpack(">HHHB", header)
                body = sock.recv(max(0, length - 1))
                if body and body[0] & 0x80:
                    print(f"[{index}] exception {body[1]:#04x}")
            except Exception as exc:
                print(f"[{index}] 錯誤 {exc!r}")
                mismatches += 1
                break
            time.sleep(args.delay)
    print(f"完成，異常 {mismatches} 筆")
    return 1 if mismatches else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Modbus 錄製與回放")
    sub = parser.add_subparsers(dest="mode", required=True)
    record = sub.add_parser("record")
    record.add_argument("--listen", default="0.0.0.0:1502")
    record.add_argument("--target", default="boiler:502")
    record.add_argument("--out", default=None)
    replay = sub.add_parser("replay")
    replay.add_argument("--target", default="boiler:502")
    replay.add_argument("--file", required=True)
    replay.add_argument("--delay", type=float, default=0.005)
    args = parser.parse_args()
    if args.mode == "record":
        return asyncio.run(_record(args))
    return _replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
