"""容器 healthcheck：確認 Modbus server 可回應 Read Input Registers。

用法：
    python -m tools.healthcheck modbus [host] [port]
    python -m tools.healthcheck http   [url]
"""
from __future__ import annotations

import socket
import struct
import sys
import urllib.request


def check_modbus(host: str = "127.0.0.1", port: int = 502, unit: int = 1) -> int:
    # 讀取 30008 Register Map Version（PDU offset 7）
    pdu = struct.pack(">BHH", 0x04, 7, 1)
    frame = struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu
    try:
        with socket.create_connection((host, port), timeout=3) as sock:
            sock.sendall(frame)
            header = sock.recv(7)
            if len(header) < 7:
                return 1
            _, _, length, _ = struct.unpack(">HHHB", header)
            body = sock.recv(max(0, length - 1))
            if not body or body[0] & 0x80:
                return 1
            return 0
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


def check_http(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "modbus"
    if mode == "http":
        sys.exit(check_http(sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080/health"))
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 502
    sys.exit(check_modbus(host, port))
