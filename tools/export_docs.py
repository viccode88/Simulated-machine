"""由程式碼產生 docs/register-map.csv 與 docs/alarm-codes.csv。

    python -m tools.export_docs

文件與實作永遠一致，避免暫存器表過期。
"""
from __future__ import annotations

import csv
import os

from common.device.base_device import common_alarms, common_protection_defs
from controller.dcs.main import DEVICE_CLASSES, build_map

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")


def export_register_map() -> str:
    rows = []
    for name in DEVICE_CLASSES:
        rows.extend(build_map(name).to_rows())
    path = os.path.join(DOCS, "register-map.csv")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def export_alarm_codes() -> str:
    rows = []
    for name, klass in DEVICE_CLASSES.items():
        for spec in list(klass.ALARMS) + common_alarms(klass.CODE_BASE):
            rows.append({
                "device": name,
                "type": "ALARM",
                "code": spec.code,
                "name": spec.name,
                "alarm_word": spec.word_index + 1,
                "bit": spec.word_bit,
                "signal": "",
                "direction": "",
                "message": spec.message,
            })
        defs = list(klass.PROTECTION_DEFS) + common_protection_defs(klass.CODE_BASE)
        for item in defs:
            rows.append({
                "device": name,
                "type": "TRIP",
                "code": item["code"],
                "name": item["name"],
                "alarm_word": "",
                "bit": "",
                "signal": item.get("signal", ""),
                "direction": item.get("direction", "high"),
                "message": item.get("message", ""),
            })
    rows.sort(key=lambda r: (r["device"], r["code"]))
    path = os.path.join(DOCS, "alarm-codes.csv")
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


if __name__ == "__main__":
    os.makedirs(DOCS, exist_ok=True)
    for path in (export_register_map(), export_alarm_codes()):
        print(f"已產生 {path}")
