"""情境檔語法驗證：確保 scenarios/*.yaml 只使用執行器支援的步驟。"""
from __future__ import annotations

import glob
import os

import pytest
import yaml

SUPPORTED = {"wait", "write", "fault", "fault_clear", "signal", "snapshot_save",
             "snapshot_restore", "expect", "expect_event", "expect_tripped",
             "check_invariants", "pause", "resume"}

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES = sorted(glob.glob(os.path.join(ROOT, "scenarios", "*.yaml")))


def test_scenarios_exist():
    assert len(FILES) >= 7, "規格要求的情境檔不足"


@pytest.mark.parametrize("path", FILES, ids=[os.path.basename(p) for p in FILES])
def test_scenario_structure(path):
    with open(path, "r", encoding="utf-8") as handle:
        scenario = yaml.safe_load(handle)
    assert scenario["name"] and scenario["description"]
    steps = scenario.get("steps") or []
    assert steps, "情境必須有步驟"
    for step in steps:
        assert len(step) == 1, f"每個步驟只能有一個動作：{step}"
        kind = next(iter(step))
        assert kind in SUPPORTED, f"{path} 使用了不支援的步驟 {kind}"
    for step in steps:
        kind, value = next(iter(step.items()))
        if kind == "expect":
            assert "signal" in value
        if kind == "write":
            assert {"device", "register", "value"} <= set(value)
        if kind in ("expect_event",):
            assert "event" in value


@pytest.mark.parametrize("path", FILES, ids=[os.path.basename(p) for p in FILES])
def test_scenario_devices_and_registers_exist(path):
    from controller.dcs.main import DEVICE_CLASSES, build_map
    from common.modbus.register_map import Table

    with open(path, "r", encoding="utf-8") as handle:
        scenario = yaml.safe_load(handle)
    for step in scenario.get("steps") or []:
        kind, value = next(iter(step.items()))
        if kind == "write":
            rmap = build_map(value["device"])
            table = Table.COIL if value.get("coil") else Table.HOLDING
            rmap.by_name(table, value["register"])
        if kind in ("fault", "fault_clear") and value.get("target") not in (None, "*", "all"):
            assert value["target"] in DEVICE_CLASSES
        if kind == "expect_tripped":
            assert value["device"] in DEVICE_CLASSES
