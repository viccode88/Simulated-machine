"""Static contract checks for the generated OpenPLC and ScadaBR integration.

These checks deliberately start from ``docs/register-map.csv``.  That file is
the plant's wire contract; neither generated artifact is allowed to become an
independent, hand-maintained address map.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import pytest


ROOT = Path(__file__).resolve().parents[2]
REGISTER_MAP = ROOT / "docs" / "register-map.csv"
OPENPLC_PROJECT = ROOT / "integrations" / "openplc" / "thermal-plant-v4"
SCADABR_DIR = ROOT / "integrations" / "scadabr"

DEVICE_ORDER = (
    "condenser",
    "condensate_pump",
    "feedwater_tank",
    "feedwater_pump",
    "boiler",
    "steam_valve",
    "turbine",
    "generator",
)
DEVICE_PORTS = {name: 15021 + index for index, name in enumerate(DEVICE_ORDER)}
CONTAINER_HOSTS = {
    "condenser": "condenser",
    "condensate_pump": "condensate-pump",
    "feedwater_tank": "feedwater-tank",
    "feedwater_pump": "feedwater-pump",
    "boiler": "boiler",
    "steam_valve": "steam-valve",
    "turbine": "turbine",
    "generator": "generator",
}
WORD_STRIDE = 64
BIT_STRIDE = 16
HOLDING_READBACK_BASE = 512

TABLE_TO_SCADA_RANGE = {
    "COIL": "COIL_STATUS",
    "DISCRETE": "INPUT_STATUS",
    "HOLDING": "HOLDING_REGISTER",
    "INPUT": "INPUT_REGISTER",
}
FUNCTION_POINT_TYPE = {
    2: "Digital Input (Discrete Input)",
    3: "Analog Input (Holding Register)",
    4: "Analog Input (Input Register)",
    15: "Digital Output (Multiple Coils)",
    16: "Analog Output (Multiple Registers)",
}
PULSE_NAMES = {
    "START",
    "STOP",
    "RESET_TRIP",
    "ACK_ALARM",
    "TRIP_TEST",
    "CLEAR_TOTALIZER",
    "BREAKER_CLOSE",
    "BREAKER_OPEN",
}
HELD_SAFETY_NAMES = {"EMERGENCY_STOP", "FORCE_SAFE"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    assert isinstance(value, dict), f"{path} must contain a JSON object"
    return value


@pytest.fixture(scope="module")
def register_rows() -> list[dict[str, str]]:
    rows = _read_csv(REGISTER_MAP)
    assert len(rows) == 688
    assert tuple(dict.fromkeys(row["device"] for row in rows)) == DEVICE_ORDER
    return rows


@pytest.fixture(scope="module")
def remote_devices() -> dict[str, dict[str, Any]]:
    paths = sorted((OPENPLC_PROJECT / "devices" / "remote").glob("*.json"))
    assert len(paths) == 8, f"expected eight OpenPLC remote devices, found {paths}"
    devices: dict[str, dict[str, Any]] = {}
    for path in paths:
        remote = _read_json(path)
        name = remote["name"]
        assert name not in devices, f"duplicate OpenPLC remote name {name!r}"
        devices[name] = remote
    assert set(devices) == set(DEVICE_ORDER)
    return devices


@pytest.fixture(scope="module")
def scadabr_export() -> dict[str, Any]:
    paths = sorted(SCADABR_DIR.glob("*.json"))
    assert len(paths) == 1, f"expected one ScadaBR import JSON, found {paths}"
    return _read_json(paths[0])


def _expected_groups(device: str) -> set[tuple[int, int, int]]:
    groups = {
        (2, 0, 16),
        (3, 0, 32),
        (4, 0, 50),
        (15, 0, 8),
        (16, 0, 14),
        (16, 19, 6),
    }
    if device == "generator":
        groups.add((15, 9, 2))
    if device != "condensate_pump":
        groups.add((16, 29, 2 if device in {"steam_valve", "turbine", "generator"} else 1))
    return groups


def _group_tuple(group: dict[str, Any]) -> tuple[int, int, int]:
    return int(group["functionCode"]), int(group["offset"]), int(group["length"])


_IEC_LOCATION_RE = re.compile(r"%([IQ])([XW])(\d+)(?:\.(\d+))?", re.IGNORECASE)


def _iec_location(location: str) -> tuple[str, int]:
    """Return the IEC area and a flat word/bit index.

    OpenPLC normally renders bit addresses as ``%IX2.3``.  The generated
    northbound map may use the equivalent flat notation ``%IX19``; accepting
    both keeps this assertion about the address rather than its presentation.
    """

    match = _IEC_LOCATION_RE.fullmatch(location.strip())
    assert match, f"invalid IEC location {location!r}"
    direction, width, major, minor = match.groups()
    area = f"{direction.upper()}{width.upper()}"
    major_value = int(major)
    if width.upper() == "X" and minor is not None:
        minor_value = int(minor)
        assert 0 <= minor_value <= 7, f"invalid IEC bit in {location!r}"
        return area, major_value * 8 + minor_value
    assert minor is None, f"word location must not have a bit suffix: {location!r}"
    return area, major_value


def _expected_local(function_code: int, device_index: int, remote_offset: int) -> tuple[str, int]:
    word_base = device_index * WORD_STRIDE
    bit_base = device_index * BIT_STRIDE
    if function_code == 2:
        return "IX", bit_base + remote_offset
    if function_code == 3:
        return "IW", HOLDING_READBACK_BASE + word_base + remote_offset
    if function_code == 4:
        return "IW", word_base + remote_offset
    if function_code == 15:
        return "QX", bit_base + remote_offset
    if function_code == 16:
        return "QW", word_base + remote_offset
    raise AssertionError(f"unexpected function code {function_code}")


def _expected_safe_qw(row: dict[str, str], device_index: int) -> int:
    scale = float(row["scale"] or 1)
    raw_minimum = (
        max(0, min(0xFFFF, int(round(float(row["min"]) * scale))))
        if row["min"]
        else 0
    )
    raw_maximum = (
        max(0, min(0xFFFF, int(round(float(row["max"]) * scale))))
        if row["max"]
        else 0xFFFF
    )
    fixed = {
        "CONTROL_MODE": 3,
        "COMMAND_SEQUENCE": 0,
        "WATCHDOG_COUNTER": 1,
        "RESET_KEY": 0,
        "SIMULATION_FLAGS": 0,
        "ACTIVE_CONTROLLER_ID": device_index + 1,
        "COMMAND_LEASE_TIME": 5,
        "OUTPUT_RATE_LIMIT": 1000,
        "RESERVED": 0,
        "OUTPUT_HIGH_LIMIT": 10000,
        "OUTPUT_LOW_LIMIT": 0,
        "CONTROLLER_SCAN_TIME": 200,
    }
    value = fixed.get(row["name"], raw_minimum)
    return max(raw_minimum, min(raw_maximum, value))


def _covered_offsets(groups: Iterable[dict[str, Any]], function_code: int) -> set[int]:
    covered: set[int] = set()
    for group in groups:
        fc, offset, length = _group_tuple(group)
        if fc == function_code:
            covered.update(range(offset, offset + length))
    return covered


def test_openplc_remote_connections_groups_locations_and_aliases(
    register_rows: list[dict[str, str]],
    remote_devices: dict[str, dict[str, Any]],
) -> None:
    aliases: list[str] = []
    point_ids: list[str] = []
    group_ids: list[str] = []
    local_owners: dict[tuple[str, int], tuple[str, int, int]] = {}

    rows_by_device: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in register_rows:
        rows_by_device[row["device"]].append(row)

    endpoints = {
        device: (
            remote_devices[device]["modbusTcpConfig"]["host"],
            int(remote_devices[device]["modbusTcpConfig"]["port"]),
        )
        for device in DEVICE_ORDER
    }
    host_mode_endpoints = {
        device: ("127.0.0.1", DEVICE_PORTS[device]) for device in DEVICE_ORDER
    }
    container_mode_endpoints = {
        device: (CONTAINER_HOSTS[device], 502) for device in DEVICE_ORDER
    }
    assert endpoints in (host_mode_endpoints, container_mode_endpoints), (
        "all remotes must consistently use either Docker service endpoints "
        "or the host's published 15021..15028 ports"
    )

    for device_index, device in enumerate(DEVICE_ORDER):
        remote = remote_devices[device]
        assert remote["protocol"] == "modbus-tcp"
        config = remote["modbusTcpConfig"]
        assert int(config["slaveId"]) == 1

        groups = config["ioGroups"]
        assert {_group_tuple(group) for group in groups} == _expected_groups(device)

        for group in groups:
            function_code, start, length = _group_tuple(group)
            points = group["ioPoints"]
            assert len(points) == length
            expected_cycle_ms = 250 if function_code in {2, 3, 4} else 100
            assert int(group["cycleTime"]) == expected_cycle_ms
            assert group["errorHandling"] == "keep-last-value"
            group_ids.append(group["id"])

            actual_locations: set[tuple[str, int]] = set()
            for point in points:
                assert point["type"] == FUNCTION_POINT_TYPE[function_code]
                location = _iec_location(point["iecLocation"])
                actual_locations.add(location)
                aliases.append(point["alias"])
                point_ids.append(point["id"])
                owner = (device, function_code, start)
                assert location not in local_owners, (
                    f"local IEC address {point['iecLocation']} is shared by "
                    f"{local_owners.get(location)} and {owner}"
                )
                local_owners[location] = owner

            expected_locations = {
                _expected_local(function_code, device_index, remote_offset)
                for remote_offset in range(start, start + length)
            }
            assert actual_locations == expected_locations

        contract = rows_by_device[device]
        for table, function_code in (("INPUT", 4), ("DISCRETE", 2), ("HOLDING", 3)):
            actual = _covered_offsets(groups, function_code)
            required = {int(row["pdu_offset"]) for row in contract if row["table"] == table}
            assert required <= actual, f"{device} FC{function_code:02d} misses {required - actual}"

        coil_writes = _covered_offsets(groups, 15)
        expected_coil_writes = {
            int(row["pdu_offset"])
            for row in contract
            if row["table"] == "COIL" and row["writable"] == "yes"
        }
        assert coil_writes == expected_coil_writes

        holding_writes = _covered_offsets(groups, 16)
        expected_holding_writes = {
            int(row["pdu_offset"])
            for row in contract
            if row["table"] == "HOLDING" and row["writable"] == "yes"
        }
        assert holding_writes == expected_holding_writes

    assert all(isinstance(alias, str) and alias.strip() for alias in aliases)
    assert len(aliases) == len(set(aliases)), "OpenPLC aliases must be globally unique"
    assert len(point_ids) == len(set(point_ids)), "OpenPLC I/O point ids must be globally unique"
    assert len(group_ids) == len(set(group_ids)), "OpenPLC I/O group ids must be globally unique"


def test_openplc_northbound_server_and_project_source(
    register_rows: list[dict[str, str]],
) -> None:
    server_paths = sorted((OPENPLC_PROJECT / "devices" / "servers").glob("*.json"))
    assert len(server_paths) == 1
    server = _read_json(server_paths[0])
    assert server["protocol"] == "modbus-tcp"
    config = server["modbusSlaveConfig"]
    assert config == {
        "enabled": True,
        "networkInterface": "0.0.0.0",
        "port": 502,
    }

    project = _read_json(OPENPLC_PROJECT / "project.json")
    tasks = project["data"]["configuration"]["resource"]["tasks"]
    instances = project["data"]["configuration"]["resource"]["instances"]
    assert tasks and instances
    assert any(str(task.get("interval", "")).upper() == "T#20MS" for task in tasks)
    assert any(instance["program"].lower() == "main" for instance in instances)

    program_paths = sorted((OPENPLC_PROJECT / "pous" / "programs").glob("*"))
    assert program_paths, "OpenPLC source project has no program"
    program_text = "\n".join(
        path.read_text(encoding="utf-8", errors="strict") for path in program_paths if path.is_file()
    ).upper()
    assert "PROGRAM MAIN" in program_text
    assert re.search(
        r"I_WATCHDOG_SCAN_COUNT\s*>=\s*10",
        program_text,
    ), "10 x 20 ms scans must advance the watchdog every 200 ms"
    assert re.search(
        r"I_[A-Z0-9_]+_SCANS\s*>=\s*8",
        program_text,
    ), "pulse commands must be held across roughly 150 ms"
    assert "WATCHDOG" in program_text
    assert "COMMAND_SEQUENCE" in program_text
    assert "RESET_KEY" in program_text
    assert "42330" in program_text or "16#A55A" in program_text
    for safety_term in (
        "BREAKER_OPEN",
        "STEAM_VALVE",
        "BOILER",
        "GENERATOR",
        "FEEDWATER_PUMP",
        "OVERSPEED",
    ):
        assert safety_term in program_text

    program_body = program_text.rsplit("END_VAR", maxsplit=1)[-1]
    init_match = re.search(
        r"IF\s+NOT\s+I_OUTPUTS_INITIALIZED\s+THEN"
        r"(?P<body>.*?)"
        r"I_OUTPUTS_INITIALIZED\s*:=\s*TRUE\s*;\s*END_IF\s*;",
        program_body,
        flags=re.DOTALL,
    )
    assert init_match, "main.st must explicitly initialize the Runtime QW image"
    init_body = init_match.group("body")
    for device_index, device in enumerate(DEVICE_ORDER):
        for row in register_rows:
            if row["device"] != device or row["table"] != "HOLDING":
                continue
            stem = f"{device}__holding__{row['name'].lower()}".upper()
            expected = _expected_safe_qw(row, device_index)
            assert re.search(
                rf"\b{re.escape(stem)}\s*:=\s*{expected}\s*;",
                init_body,
            ), f"{device}.{row['name']} has no executable safe QW initialization"

    pulse_rows = [row for row in register_rows if row["table"] == "COIL" and row["pulse"] == "yes"]
    assert len(pulse_rows) == 50
    for row in pulse_rows:
        stem = f"{row['device']}__coil__{row['name'].lower()}".upper()
        state = f"I_{row['device']}_{row['name'].lower()}_ACTIVE".upper()
        assert state in program_text, f"{row['device']}.{row['name']} has no pulse state"
        assert re.search(
            rf"\b{re.escape(stem)}\s*:=\s*FALSE\s*;",
            program_body,
        ), f"{row['device']}.{row['name']} is never cleared after its pulse"

    for device in DEVICE_ORDER:
        for held_name in HELD_SAFETY_NAMES:
            stem = f"{device}__coil__{held_name.lower()}".upper()
            assert not re.search(
                rf"\b{re.escape(stem)}\s*:=\s*FALSE\s*;",
                program_body,
            ), f"{device}.{held_name} must remain latched until the HMI releases it"
    assert program_body.count("__HOLDING__RESET_KEY := 42330") == 8


def _northbound_location_field(row: dict[str, str]) -> str:
    for name in ("iec_location", "openplc_location", "local_iec_location", "local_iec"):
        if name in row:
            return row[name]
    raise AssertionError(
        "northbound-map.csv must include iec_location, openplc_location, "
        "or local_iec_location"
    )


def _expected_northbound(table: str, device_index: int, pdu_offset: int) -> tuple[str, int]:
    if table == "INPUT":
        return "IW", device_index * WORD_STRIDE + pdu_offset
    if table == "DISCRETE":
        return "IX", device_index * BIT_STRIDE + pdu_offset
    if table == "HOLDING":
        return "QW", device_index * WORD_STRIDE + pdu_offset
    if table == "COIL":
        return "QX", device_index * BIT_STRIDE + pdu_offset
    raise AssertionError(f"unknown table {table!r}")


def test_northbound_map_is_a_complete_projection_of_register_map(
    register_rows: list[dict[str, str]],
) -> None:
    generated = _read_csv(OPENPLC_PROJECT / "northbound-map.csv")
    primary = [row for row in generated if row["source_table"] != "HOLDING_READBACK"]
    readback = [row for row in generated if row["source_table"] == "HOLDING_READBACK"]
    identity = lambda row: (
        row["device"],
        row.get("table", row.get("source_table")),
        int(row.get("pdu_offset", row.get("remote_pdu_offset", -1))),
        row["name"],
    )
    expected_by_identity = {identity(row): row for row in register_rows}
    generated_by_identity = {identity(row): row for row in primary}
    assert len(expected_by_identity) == len(register_rows)
    assert len(generated_by_identity) == len(primary)
    assert generated_by_identity.keys() == expected_by_identity.keys()
    assert len(primary) == 688
    assert len(readback) == 170
    assert len(generated) == 858

    device_indexes = {device: index for index, device in enumerate(DEVICE_ORDER)}
    for key, generated_row in generated_by_identity.items():
        source_row = expected_by_identity[key]
        expected_location = _expected_northbound(
            source_row["table"],
            device_indexes[source_row["device"]],
            int(source_row["pdu_offset"]),
        )
        assert _iec_location(_northbound_location_field(generated_row)) == expected_location
        for field in ("dtype", "unit", "scale", "writable", "pulse"):
            if field in generated_row:
                if field == "scale":
                    assert float(generated_row[field]) == float(source_row[field])
                else:
                    assert generated_row[field] == source_row[field]

    holding_by_identity = {
        (row["device"], int(row["pdu_offset"]), row["name"]): row
        for row in register_rows
        if row["table"] == "HOLDING"
    }
    assert {
        (
            row["device"],
            int(row["remote_pdu_offset"]),
            row["name"],
        )
        for row in readback
    } == holding_by_identity.keys()
    for row in readback:
        device_index = device_indexes[row["device"]]
        remote_offset = int(row["remote_pdu_offset"])
        assert _iec_location(_northbound_location_field(row)) == (
            "IW",
            HOLDING_READBACK_BASE + device_index * WORD_STRIDE + remote_offset,
        )
        assert row["northbound_table"] == "INPUT"
        assert int(row["northbound_pdu_offset"]) == (
            HOLDING_READBACK_BASE + device_index * WORD_STRIDE + remote_offset
        )
        assert row["writable"] == "no"


def _is_u32_low_word(rows: list[dict[str, str]], index: int) -> bool:
    if index == 0:
        return False
    row = rows[index]
    previous = rows[index - 1]
    return (
        row["name"].endswith("_LO")
        and previous["dtype"] == "u32"
        and previous["name"].endswith("_HI")
        and row["device"] == previous["device"]
        and row["table"] == previous["table"]
        and int(row["pdu_offset"]) == int(previous["pdu_offset"]) + 1
    )


def _scada_dtype(row: dict[str, str]) -> str:
    if row["table"] in {"COIL", "DISCRETE"}:
        return "BINARY"
    if row["dtype"] == "i16":
        return "TWO_BYTE_INT_SIGNED"
    if row["dtype"] == "u32":
        return "FOUR_BYTE_INT_UNSIGNED"
    return "TWO_BYTE_INT_UNSIGNED"


def _scada_offset(row: dict[str, str], device_indexes: dict[str, int]) -> int:
    stride = BIT_STRIDE if row["table"] in {"COIL", "DISCRETE"} else WORD_STRIDE
    return device_indexes[row["device"]] * stride + int(row["pdu_offset"])


def _point_identity(point: dict[str, Any]) -> tuple[str, str, str]:
    range_to_table = {value: key for key, value in TABLE_TO_SCADA_RANGE.items()}
    return (
        str(point["deviceName"]),
        range_to_table[str(point["pointLocator"]["range"])],
        str(point["name"]),
    )


def test_scadabr_data_source_and_all_661_points(
    register_rows: list[dict[str, str]],
    scadabr_export: dict[str, Any],
) -> None:
    data_sources = scadabr_export["dataSources"]
    assert len(data_sources) == 1
    data_source = data_sources[0]
    assert data_source["type"] == "MODBUS_IP"
    assert data_source["transportType"] == "TCP"
    assert data_source["host"] == "localhost"
    assert int(data_source["port"]) == 502
    assert data_source["enabled"] is True
    assert data_source["updatePeriodType"] == "MILLISECONDS"
    assert int(data_source["updatePeriods"]) == 250
    assert int(data_source["timeout"]) >= 250
    assert int(data_source["retries"]) >= 1

    for private_key in ("users", "systemSettings", "pointValues"):
        assert not scadabr_export.get(private_key), f"safe import must not carry {private_key}"

    expected_rows = [
        row for index, row in enumerate(register_rows) if not _is_u32_low_word(register_rows, index)
    ]
    assert len(expected_rows) == 661
    points = scadabr_export["dataPoints"]
    assert len(points) == 661

    xids = [point["xid"] for point in points]
    assert len(xids) == len(set(xids))
    assert all(isinstance(xid, str) and 1 <= len(xid) <= 50 for xid in xids)
    assert all(point["dataSourceXid"] == data_source["xid"] for point in points)

    expected = {(row["device"], row["table"], row["name"]): row for row in expected_rows}
    actual = {_point_identity(point): point for point in points}
    assert len(actual) == len(points), "duplicate ScadaBR device/name identity"
    assert actual.keys() == expected.keys()

    device_indexes = {device: index for index, device in enumerate(DEVICE_ORDER)}
    for identity, row in expected.items():
        point = actual[identity]
        locator = point["pointLocator"]
        assert point["enabled"] is True
        assert locator["range"] == TABLE_TO_SCADA_RANGE[row["table"]]
        assert locator["modbusDataType"] == _scada_dtype(row)
        assert int(locator["offset"]) == _scada_offset(row, device_indexes)
        assert int(locator["slaveId"]) == 1
        assert bool(locator["settableOverride"]) is (row["writable"] == "yes")
        assert math.isclose(
            float(locator["multiplier"]),
            1.0 / float(row["scale"]),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        assert math.isclose(float(locator["additive"]), 0.0, abs_tol=0.0)
        assert point["engineeringUnits"] == row["unit"]

    excluded_lows = {
        (row["device"], row["table"], row["name"])
        for index, row in enumerate(register_rows)
        if _is_u32_low_word(register_rows, index)
    }
    assert excluded_lows.isdisjoint(actual)
    assert Counter(
        point["pointLocator"]["modbusDataType"] for point in points
    )["FOUR_BYTE_INT_UNSIGNED"] == 27


def _walk_components(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "type" in value and ("dataPointXid" in value or "x" in value or "y" in value):
            yield value
        for child in value.values():
            yield from _walk_components(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_components(child)


def _normal_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _view_for_device(views: list[dict[str, Any]], device: str) -> dict[str, Any]:
    needle = _normal_name(device)
    matches = [view for view in views if needle in _normal_name(str(view["name"]))]
    assert len(matches) == 1, f"expected one view for {device}, got {[v['name'] for v in matches]}"
    return matches[0]


def _script_writes_boolean(script: str, boolean: str) -> bool:
    return bool(
        re.search(
            rf"setPoint\s*\([^;]*,\s*{boolean}\s*\)",
            script,
            flags=re.IGNORECASE,
        )
    )


def test_scadabr_references_views_watchlists_and_safe_controls(
    register_rows: list[dict[str, str]],
    scadabr_export: dict[str, Any],
) -> None:
    points = scadabr_export["dataPoints"]
    point_by_xid = {point["xid"]: point for point in points}
    data_source_xids = {source["xid"] for source in scadabr_export["dataSources"]}
    assert all(point["dataSourceXid"] in data_source_xids for point in points)

    watchlists = scadabr_export["watchLists"]
    views = scadabr_export["graphicalViews"]
    assert len(watchlists) == 9
    assert len(views) == 9
    assert len({watchlist["xid"] for watchlist in watchlists}) == 9
    assert len({view["xid"] for view in views}) == 9
    assert len({watchlist["name"] for watchlist in watchlists}) == 9
    assert len({view["name"] for view in views}) == 9

    for watchlist in watchlists:
        assert watchlist["dataPoints"]
        assert set(watchlist["dataPoints"]) <= point_by_xid.keys()
    for device in DEVICE_ORDER:
        expected_device_points = {
            point["xid"] for point in points if point["deviceName"] == device
        }
        assert sum(
            set(watchlist["dataPoints"]) == expected_device_points for watchlist in watchlists
        ) == 1, f"expected one complete {device} watchlist"

    components = list(_walk_components(views))
    assert components
    for component in components:
        xid = component.get("dataPointXid")
        if xid is not None:
            assert xid in point_by_xid, f"view component refers to missing point {xid}"

    row_by_identity = {
        (row["device"], row["table"], row["name"]): row for row in register_rows
    }
    for component in components:
        xid = component.get("dataPointXid")
        if xid is None:
            continue
        point = point_by_xid[xid]
        row = row_by_identity[_point_identity(point)]
        if component.get("settableOverride"):
            assert row["writable"] == "yes"

        component_type = component["type"]
        if row["table"] == "COIL" and component.get("settableOverride"):
            expected_component = "SCRIPT" if row["pulse"] == "yes" else "BUTTON"
            assert component_type == expected_component
        if component_type == "SCRIPT":
            assert row["table"] == "COIL"
            assert row["pulse"] == "yes"
            assert row["name"] in PULSE_NAMES
            assert component.get("settableOverride") is True
            script = str(component.get("script", ""))
            assert _script_writes_boolean(script, "true")
            assert not _script_writes_boolean(script, "false")
        elif component_type == "BUTTON":
            assert row["table"] == "COIL"
            assert row["pulse"] == "no"
            assert row["name"] in HELD_SAFETY_NAMES
            assert component.get("settableOverride") is True

    for device in DEVICE_ORDER:
        view = _view_for_device(views, device)
        view_components = list(_walk_components(view.get("viewComponents", [])))
        referenced = {
            _point_identity(point_by_xid[component["dataPointXid"]])[2]: component["type"]
            for component in view_components
            if component.get("dataPointXid") in point_by_xid
        }
        assert referenced.get("EMERGENCY_STOP") == "BUTTON"
        assert referenced.get("FORCE_SAFE") == "BUTTON"
        assert referenced.get("RESET_TRIP") == "SCRIPT"
        assert referenced.get("ACK_ALARM") == "SCRIPT"
        if device == "feedwater_tank":
            assert "START" not in referenced
            assert "STOP" not in referenced
        else:
            assert referenced.get("START") == "SCRIPT"
            assert referenced.get("STOP") == "SCRIPT"
        if device == "generator":
            assert referenced.get("BREAKER_CLOSE") == "SCRIPT"
            assert referenced.get("BREAKER_OPEN") == "SCRIPT"
