"""控制器啟動路徑的回歸測試。

pymodbus 的 AsyncModbusTcpClient 在 __init__ 就呼叫 asyncio.get_running_loop()，
而 DCS 與外部 PLC 都是在 asyncio.run() 之前建構的，
所以連線物件必須延後到 connect() 才建立，否則容器一啟動就 RuntimeError。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "configs")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _build_dcs(tmp_path):
    os.environ["STATE_DIR"] = str(tmp_path)
    from controller.dcs.main import DCS

    return DCS(config_dir=CONFIG_DIR)


def test_dcs_constructs_without_running_event_loop(tmp_path):
    """複製 controller/dcs/main.py 的 main()：先建物件，之後才 asyncio.run。"""
    dcs = _run_outside_loop(lambda: _build_dcs(tmp_path))
    assert len(dcs.devices) == 8
    # 連線物件尚未建立，等 connect() 才建
    assert all(link.client is None for link in dcs.devices.values())


def test_device_link_write_is_safe_before_connect(tmp_path):
    dcs = _run_outside_loop(lambda: _build_dcs(tmp_path))
    link = dcs.devices["boiler"]
    assert asyncio.run(link.write_hr("MANUAL_OUTPUT", 50.0)) is False
    assert asyncio.run(link.pulse_coil("START")) is False
    assert asyncio.run(link.poll()) is False


def test_external_plc_example_selftest_passes():
    """examples/external_plc.py 的離線自我檢查（CSV 解析、編解碼、PID）。"""
    sys.path.insert(0, os.path.join(ROOT, "examples"))
    try:
        import external_plc
    finally:
        sys.path.pop(0)

    rmap = external_plc.load_register_map(os.path.join(ROOT, "docs", "register-map.csv"))
    assert external_plc.selftest(rmap) == 0

    # 範例中引用的暫存器名稱都必須真的存在於介面契約內
    for source, actions in external_plc.TRIP_MATRIX.items():
        assert source in rmap
        for device, kind, target, _ in actions:
            table = "HOLDING" if kind == "hr" else "COIL"
            assert (table, target) in rmap[device], f"{device}.{target} 不在 register map"


def _run_outside_loop(factory):
    """確保建構時沒有執行中的事件迴圈。"""
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()
    return factory()
