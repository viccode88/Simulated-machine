"""文件地址 vs PDU offset、共通映射完整性。"""
import pytest

from common.modbus.register_map import RegisterMap, Table
from controller.dcs.main import DEVICE_CLASSES, build_map


def test_doc_address_vs_pdu_offset():
    rmap = build_map("boiler")
    assert rmap.by_name(Table.HOLDING, "CONTROL_MODE").doc_address(Table.HOLDING) == 40001
    assert rmap.by_name(Table.HOLDING, "PRIMARY_SETPOINT").doc_address(Table.HOLDING) == 40010
    assert rmap.by_name(Table.HOLDING, "PRIMARY_SETPOINT").offset == 9
    assert rmap.by_name(Table.INPUT, "BOILER_PRESSURE").doc_address(Table.INPUT) == 30010
    assert rmap.by_name(Table.COIL, "START").doc_address(Table.COIL) == 1
    assert rmap.by_name(Table.DISCRETE, "READY").doc_address(Table.DISCRETE) == 10001


@pytest.mark.parametrize("name", sorted(DEVICE_CLASSES))
def test_every_device_has_common_registers(name):
    rmap = build_map(name)
    for coil in ("START", "STOP", "RESET_TRIP", "ACK_ALARM", "EMERGENCY_STOP"):
        assert rmap.by_name(Table.COIL, coil).writable
    for reg in ("STATUS_WORD", "TRIP_WORD", "FIRST_OUT_CODE", "OVERALL_QUALITY",
                "REGISTER_MAP_VERSION", "WATCHDOG_ECHO", "SNAPSHOT_GENERATION"):
        rmap.by_name(Table.INPUT, reg)
    for reg in ("CONTROL_MODE", "WATCHDOG_COUNTER", "RESET_KEY", "COMMAND_SEQUENCE"):
        assert rmap.by_name(Table.HOLDING, reg).writable


@pytest.mark.parametrize("name", sorted(DEVICE_CLASSES))
def test_process_values_start_at_30010(name):
    rmap = build_map(name)
    process = [s for s in rmap.inputs.values() if 9 <= s.offset <= 28]
    assert process, f"{name} 缺少程序量"
    assert all(s.doc_address(Table.INPUT) >= 30010 for s in process)


def test_generator_breaker_coils():
    rmap = build_map("generator")
    assert rmap.by_name(Table.COIL, "BREAKER_CLOSE").doc_address(Table.COIL) == 10
    assert rmap.by_name(Table.COIL, "BREAKER_OPEN").doc_address(Table.COIL) == 11


def test_register_map_csv_rows():
    rows = build_map("turbine").to_rows()
    assert any(r["name"] == "SPEED_RPM" and r["doc_address"] == 30010 for r in rows)
    assert all({"device", "table", "doc_address", "pdu_offset", "name"} <= set(r) for r in rows)
