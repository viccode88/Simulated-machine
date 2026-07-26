#!/usr/bin/env python3
"""Generate the ScadaBR 1.2 import for the flat OpenPLC northbound image.

The source of truth is docs/register-map.csv.  The generated JSON deliberately
contains only the four root arrays needed by this integration:

* graphicalViews
* dataSources
* dataPoints
* watchLists

No users, system settings, historical point values, or point hierarchy are
copied from an existing ScadaBR installation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTER_MAP = REPO_ROOT / "docs" / "register-map.csv"
DEFAULT_OUTPUT = REPO_ROOT / "integrations" / "scadabr" / "scadabr.json"

DEVICES: Tuple[str, ...] = (
    "condenser",
    "condensate_pump",
    "feedwater_tank",
    "feedwater_pump",
    "boiler",
    "steam_valve",
    "turbine",
    "generator",
)
DEVICE_LABELS: Mapping[str, str] = {
    "condenser": "冷凝器 / Condenser",
    "condensate_pump": "凝結水泵 / Condensate Pump",
    "feedwater_tank": "給水槽 / Feedwater Tank",
    "feedwater_pump": "給水泵 / Feedwater Pump",
    "boiler": "鍋爐 / Boiler",
    "steam_valve": "蒸汽閥 / Steam Valve",
    "turbine": "汽輪機 / Turbine",
    "generator": "發電機 / Generator",
}
DEVICE_CODES: Mapping[str, str] = {
    "condenser": "CD",
    "condensate_pump": "CP",
    "feedwater_tank": "FT",
    "feedwater_pump": "FP",
    "boiler": "BL",
    "steam_valve": "SV",
    "turbine": "TB",
    "generator": "GN",
}

TABLES: Tuple[str, ...] = ("COIL", "DISCRETE", "INPUT", "HOLDING")
TABLE_CODES: Mapping[str, str] = {
    "COIL": "C",
    "DISCRETE": "D",
    "INPUT": "I",
    "HOLDING": "H",
}
RANGES: Mapping[str, str] = {
    "COIL": "COIL_STATUS",
    "DISCRETE": "INPUT_STATUS",
    "INPUT": "INPUT_REGISTER",
    "HOLDING": "HOLDING_REGISTER",
}
WORD_TABLES = frozenset(("INPUT", "HOLDING"))
BIT_TABLES = frozenset(("COIL", "DISCRETE"))
WRITABLE_TABLES = frozenset(("COIL", "HOLDING"))
WORD_BLOCK = 64
BIT_BLOCK = 16
DATA_SOURCE_XID = "DS_TPS_OPENPLC_FLAT"

REQUIRED_COLUMNS = (
    "device",
    "table",
    "doc_address",
    "pdu_offset",
    "name",
    "dtype",
    "unit",
    "scale",
    "min",
    "max",
    "writable",
    "pulse",
    "description",
)

KEY_PVS: Mapping[str, Tuple[str, ...]] = {
    "condenser": (
        "CONDENSER_PRESSURE",
        "VACUUM",
        "HOTWELL_LEVEL",
        "EXHAUST_INFLOW",
        "CONDENSATE_TEMPERATURE",
        "HOTWELL_MASS_HI",
    ),
    "condensate_pump": (
        "ACTUAL_SPEED",
        "FLOW",
        "SUCTION_PRESSURE",
        "DISCHARGE_PRESSURE",
        "MOTOR_CURRENT",
        "VIBRATION",
    ),
    "feedwater_tank": (
        "TANK_LEVEL",
        "TANK_MASS_HI",
        "CONDENSATE_INFLOW",
        "FEEDWATER_OUTFLOW",
        "WATER_TEMPERATURE",
        "TANK_PRESSURE",
        "NET_FLOW",
    ),
    "feedwater_pump": (
        "ACTUAL_SPEED",
        "FLOW",
        "SUCTION_PRESSURE",
        "DISCHARGE_PRESSURE",
        "MOTOR_CURRENT",
        "BOILER_PRESSURE",
        "FEEDWATER_PERMITTED",
    ),
    "boiler": (
        "BOILER_PRESSURE",
        "LEVEL_INDICATED",
        "LEVEL_ACTUAL",
        "FEEDWATER_FLOW",
        "STEAM_GENERATION",
        "STEAM_OUTFLOW",
        "BURNER_OUTPUT",
        "STEAM_TEMPERATURE",
        "FLAME_STATUS",
        "PURGE_TIME_REMAINING",
    ),
    "steam_valve": (
        "COMMAND_POSITION",
        "ACTUAL_POSITION",
        "UPSTREAM_PRESSURE",
        "DOWNSTREAM_PRESSURE",
        "STEAM_FLOW",
        "POSITION_DEVIATION",
        "FAST_CLOSE_STATUS",
    ),
    "turbine": (
        "SPEED_RPM",
        "MECHANICAL_POWER",
        "STEAM_FLOW",
        "MAIN_STEAM_PRESSURE",
        "EXHAUST_PRESSURE",
        "VIBRATION",
        "BEARING_TEMPERATURE",
        "GOVERNOR_OUTPUT",
        "ACCELERATION",
    ),
    "generator": (
        "ELECTRICAL_POWER",
        "LOAD_DEMAND",
        "FREQUENCY",
        "VOLTAGE",
        "CURRENT",
        "POWER_FACTOR",
        "BREAKER_STATUS",
        "SYNC_PERMISSIVE",
        "PHASE_ANGLE_DIFF",
        "OPERATING_MODE",
    ),
}

# These are the controls documented as having a direct process effect in
# docs/plc-integration.md section 5.1.  Generic but unused placeholders such as
# SECONDARY_SETPOINT are intentionally not put on the HMI.
MAJOR_SETTINGS: Mapping[str, Tuple[str, ...]] = {
    "condenser": ("CONTROL_MODE", "MANUAL_OUTPUT", "MAKEUP_VALVE_CMD"),
    "condensate_pump": ("CONTROL_MODE", "MANUAL_OUTPUT"),
    "feedwater_tank": ("CONTROL_MODE", "MANUAL_OUTPUT", "HEATING_SETPOINT"),
    "feedwater_pump": ("CONTROL_MODE", "MANUAL_OUTPUT", "OUTLET_VALVE_CMD"),
    "boiler": (
        "CONTROL_MODE",
        "MANUAL_OUTPUT",
        "PRIMARY_SETPOINT",
        "BLOWDOWN_VALVE_CMD",
    ),
    "steam_valve": (
        "CONTROL_MODE",
        "MANUAL_OUTPUT",
        "OPEN_RATE",
        "CLOSE_RATE",
    ),
    "turbine": (
        "CONTROL_MODE",
        "MANUAL_OUTPUT",
        "INERTIA_PARAM",
        "DAMPING_PARAM",
    ),
    "generator": (
        "CONTROL_MODE",
        "PRIMARY_SETPOINT",
        "OPERATING_MODE",
        "LOAD_RATE_LIMIT",
    ),
}

STATUS_POINTS: Tuple[Tuple[str, str], ...] = (
    ("READY", "READY"),
    ("RUNNING", "RUNNING"),
    ("STARTING", "STARTING"),
    ("STOPPING", "STOPPING"),
    ("TRIPPED", "TRIPPED"),
    ("ALARM_ACTIVE", "ALARM"),
    ("CONTROL_WATCHDOG_OK", "WATCHDOG"),
    ("SIM_BUS_OK", "SIM BUS"),
    ("INTERLOCKS_OK", "INTERLOCKS"),
    ("SENSOR_FAULT", "SENSOR FAULT"),
    ("ACTUATOR_FAULT", "ACTUATOR FAULT"),
    ("DEVICE_STATE", "DEVICE STATE"),
    ("FIRST_OUT_CODE", "FIRST OUT"),
    ("OVERALL_QUALITY", "QUALITY"),
    ("ACCEPTED_COMMAND_COUNT", "ACCEPTED CMDS"),
    ("REJECTED_COMMAND_COUNT", "REJECTED CMDS"),
)

OVERVIEW_STATUS_NAMES: Tuple[str, ...] = (
    "READY",
    "RUNNING",
    "TRIPPED",
    "ALARM_ACTIVE",
    "CONTROL_WATCHDOG_OK",
    "SIM_BUS_OK",
    "OVERALL_QUALITY",
    "DEVICE_STATE",
)

ROOT_KEYS = frozenset(("graphicalViews", "dataSources", "dataPoints", "watchLists"))
XID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,49}$")
MAX_DOUBLE = 1.7976931348623157e308


@dataclass(frozen=True)
class Register:
    device: str
    table: str
    doc_address: int
    pdu_offset: int
    name: str
    dtype: str
    unit: str
    scale: float
    minimum: str
    maximum: str
    writable: bool
    pulse: bool
    description: str
    source_line: int

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.device, self.table, self.name)


def _yes_no(value: str, field: str, line: int) -> bool:
    normalized = value.strip().lower()
    if normalized not in ("yes", "no"):
        raise ValueError(f"line {line}: {field} must be yes or no, got {value!r}")
    return normalized == "yes"


def load_registers(path: Path) -> List[Register]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REQUIRED_COLUMNS:
            raise ValueError(
                f"{path}: expected columns {REQUIRED_COLUMNS}, got {reader.fieldnames}"
            )

        rows: List[Register] = []
        seen_keys = set()
        for line, raw in enumerate(reader, start=2):
            device = raw["device"].strip()
            table = raw["table"].strip().upper()
            name = raw["name"].strip().upper()
            dtype = raw["dtype"].strip().lower()
            if device not in DEVICES:
                raise ValueError(f"line {line}: unknown device {device!r}")
            if table not in TABLES:
                raise ValueError(f"line {line}: unknown table {table!r}")
            if dtype not in ("u16", "i16", "u32", "enum", "bitfield"):
                raise ValueError(f"line {line}: unsupported dtype {dtype!r}")

            try:
                doc_address = int(raw["doc_address"])
                pdu_offset = int(raw["pdu_offset"])
                scale = float(raw["scale"])
            except ValueError as exc:
                raise ValueError(f"line {line}: invalid numeric field: {exc}") from exc
            if pdu_offset < 0:
                raise ValueError(f"line {line}: pdu_offset cannot be negative")
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError(f"line {line}: scale must be finite and positive")
            if table in WORD_TABLES and pdu_offset >= WORD_BLOCK:
                raise ValueError(f"line {line}: {table} exceeds {WORD_BLOCK}-word block")
            if table in BIT_TABLES and pdu_offset >= BIT_BLOCK:
                raise ValueError(f"line {line}: {table} exceeds {BIT_BLOCK}-bit block")

            key = (device, table, name)
            if key in seen_keys:
                raise ValueError(f"line {line}: duplicate register {key}")
            seen_keys.add(key)
            rows.append(
                Register(
                    device=device,
                    table=table,
                    doc_address=doc_address,
                    pdu_offset=pdu_offset,
                    name=name,
                    dtype=dtype,
                    unit=raw["unit"].strip(),
                    scale=scale,
                    minimum=raw["min"].strip(),
                    maximum=raw["max"].strip(),
                    writable=_yes_no(raw["writable"], "writable", line),
                    pulse=_yes_no(raw["pulse"], "pulse", line),
                    description=raw["description"].strip(),
                    source_line=line,
                )
            )

    rows.sort(
        key=lambda row: (
            DEVICES.index(row.device),
            TABLES.index(row.table),
            row.pdu_offset,
            row.name,
        )
    )
    if tuple(dict.fromkeys(row.device for row in rows)) != DEVICES:
        raise ValueError("register map must contain all devices in the canonical order")
    return rows


def point_registers(registers: Sequence[Register]) -> List[Register]:
    """Return registers that become ScadaBR points, folding u32 HI/LO pairs."""

    skipped_lines = set()
    result: List[Register] = []
    for index, row in enumerate(registers):
        if row.source_line in skipped_lines:
            continue
        if row.dtype != "u32":
            if row.name.endswith("_LO"):
                raise ValueError(
                    f"line {row.source_line}: orphan _LO row {row.device}.{row.name}"
                )
            result.append(row)
            continue

        if not row.name.endswith("_HI"):
            raise ValueError(
                f"line {row.source_line}: u32 point name must end with _HI"
            )
        if index + 1 >= len(registers):
            raise ValueError(f"line {row.source_line}: u32 has no following _LO row")
        low = registers[index + 1]
        expected_low_name = row.name[:-3] + "_LO"
        if (
            low.device != row.device
            or low.table != row.table
            or low.pdu_offset != row.pdu_offset + 1
            or low.name != expected_low_name
            or low.dtype != "u16"
        ):
            raise ValueError(
                f"line {row.source_line}: u32 {row.name} must be followed by "
                f"{expected_low_name} at PDU offset {row.pdu_offset + 1}"
            )
        skipped_lines.add(low.source_line)
        result.append(row)
    return result


def flat_offset(row: Register) -> int:
    device_index = DEVICES.index(row.device)
    block = WORD_BLOCK if row.table in WORD_TABLES else BIT_BLOCK
    return device_index * block + row.pdu_offset


def point_xid(row: Register) -> str:
    identity = (
        f"thermal-plant|{row.device}|{row.table}|{row.pdu_offset}|{row.name}"
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10].upper()
    return (
        f"DP_TPS_{DEVICE_CODES[row.device]}_{TABLE_CODES[row.table]}_"
        f"{row.pdu_offset:02d}_{digest}"
    )


def _modbus_data_type(row: Register) -> str:
    if row.table in BIT_TABLES:
        return "BINARY"
    if row.dtype == "u32":
        # Modbus4J's non-SWAPPED four-byte type consumes the addressed high
        # word first, matching the simulator's documented High Word First map.
        return "FOUR_BYTE_INT_UNSIGNED"
    if row.dtype == "i16":
        return "TWO_BYTE_INT_SIGNED"
    return "TWO_BYTE_INT_UNSIGNED"


def _numeric_format(scale: float) -> str:
    rounded = round(scale)
    if math.isclose(scale, rounded):
        if rounded == 1:
            return "#,##0"
        digits = int(round(math.log10(rounded))) if rounded > 0 else 0
        if rounded == 10**digits and 1 <= digits <= 6:
            return "#,##0." + ("0" * digits)
    return "#,##0.######"


def _binary_renderer(name: str) -> Dict[str, Any]:
    labels = {
        "READY": ("NOT READY", "#dc2626", "READY", "#16a34a"),
        "RUNNING": ("STOPPED", "#6b7280", "RUNNING", "#16a34a"),
        "STARTING": ("NO", "#6b7280", "STARTING", "#2563eb"),
        "STOPPING": ("NO", "#6b7280", "STOPPING", "#d97706"),
        "TRIPPED": ("NORMAL", "#16a34a", "TRIPPED", "#dc2626"),
        "ALARM_ACTIVE": ("NORMAL", "#16a34a", "ALARM", "#dc2626"),
        "INTERLOCKS_OK": ("NOT OK", "#dc2626", "OK", "#16a34a"),
        "CONTROL_WATCHDOG_OK": ("LOST", "#dc2626", "OK", "#16a34a"),
        "SIM_BUS_OK": ("BAD", "#dc2626", "OK", "#16a34a"),
        "SENSOR_FAULT": ("NORMAL", "#16a34a", "FAULT", "#dc2626"),
        "ACTUATOR_FAULT": ("NORMAL", "#16a34a", "FAULT", "#dc2626"),
        "BREAKER_STATUS": ("OPEN", "#6b7280", "CLOSED", "#16a34a"),
        "SYNC_PERMISSIVE": ("BLOCKED", "#dc2626", "PERMISSIVE", "#16a34a"),
    }
    zero_label, zero_colour, one_label, one_colour = labels.get(
        name, ("OFF", "#6b7280", "ON", "#16a34a")
    )
    return {
        "type": "BINARY",
        "zeroLabel": zero_label,
        "zeroColour": zero_colour,
        "oneLabel": one_label,
        "oneColour": one_colour,
    }


def _text_renderer(row: Register) -> Dict[str, Any]:
    if row.table in BIT_TABLES:
        return _binary_renderer(row.name)
    suffix = f" {row.unit}" if row.unit else ""
    return {
        "type": "ANALOG",
        "format": _numeric_format(row.scale),
        "suffix": suffix,
    }


def build_data_point(row: Register) -> Dict[str, Any]:
    settable = row.table in WRITABLE_TABLES and row.writable
    multiplier = 1.0 if row.table in BIT_TABLES else 1.0 / row.scale
    return {
        "xid": point_xid(row),
        "loggingType": "ON_CHANGE",
        "intervalLoggingPeriodType": "MINUTES",
        "intervalLoggingType": "INSTANT",
        "purgeType": "YEARS",
        "pointLocator": {
            "range": RANGES[row.table],
            "modbusDataType": _modbus_data_type(row),
            "additive": 0.0,
            "bit": 0,
            "charset": "ASCII",
            "multiplier": multiplier,
            "offset": flat_offset(row),
            "registerCount": 0,
            "settableOverride": settable,
            "slaveId": 1,
            "slaveMonitor": False,
            "socketMonitor": False,
        },
        "eventDetectors": [],
        "engineeringUnits": row.unit,
        "chartColour": None,
        "chartRenderer": None,
        "dataSourceXid": DATA_SOURCE_XID,
        "defaultCacheSize": 1,
        "deviceName": row.device,
        "discardExtremeValues": False,
        "discardHighLimit": MAX_DOUBLE,
        "discardLowLimit": -MAX_DOUBLE,
        "enabled": True,
        "intervalLoggingPeriod": 15,
        "name": row.name,
        "purgePeriod": 1,
        "textRenderer": _text_renderer(row),
        "tolerance": 0.0,
    }


def build_data_source(host: str) -> Dict[str, Any]:
    return {
        "xid": DATA_SOURCE_XID,
        "type": "MODBUS_IP",
        "alarmLevels": {
            "POINT_WRITE_EXCEPTION": "URGENT",
            "DATA_SOURCE_EXCEPTION": "URGENT",
            "POINT_READ_EXCEPTION": "URGENT",
        },
        "updatePeriodType": "MILLISECONDS",
        "transportType": "TCP",
        "contiguousBatches": False,
        "createSlaveMonitorPoints": False,
        "createSocketMonitorPoint": False,
        "enabled": True,
        "encapsulated": False,
        "host": host,
        "maxReadBitCount": 2000,
        "maxReadRegisterCount": 125,
        "maxWriteRegisterCount": 120,
        "name": "OpenPLC Flat Thermal Plant Gateway",
        "port": 502,
        "quantize": False,
        "retries": 2,
        "timeout": 1000,
        "updatePeriods": 250,
    }


def _lookup(
    by_key: Mapping[Tuple[str, str, str], Register],
    device: str,
    name: str,
    preferred_tables: Iterable[str] = TABLES,
) -> Register:
    for table in preferred_tables:
        row = by_key.get((device, table, name))
        if row is not None:
            return row
    raise ValueError(f"HMI references missing point {device}.{name}")


def _html_component(x: int, y: int, content: str) -> Dict[str, Any]:
    return {"type": "HTML", "x": x, "y": y, "content": content}


def _simple_component(
    row: Register,
    x: int,
    y: int,
    label: str,
    *,
    settable: bool = False,
) -> Dict[str, Any]:
    return {
        "type": "SIMPLE",
        "x": x,
        "y": y,
        "dataPointXid": point_xid(row),
        "nameOverride": label,
        "settableOverride": settable,
        "bkgdColorOverride": None,
        "displayControls": settable,
        "displayPointName": True,
        "styleAttribute": "font-family:Arial,sans-serif;font-size:14px;",
    }


def _pulse_component(
    row: Register, x: int, y: int, label: str
) -> Dict[str, Any]:
    escaped_label = html.escape(label, quote=True)
    script = (
        "return \"<input type='button' value='"
        + escaped_label
        + "' onclick='mango.view.setPoint(\" + point.id + \",\" + "
        "pointComponent.id + \",true);return false;' "
        "style='width:130px;height:32px;font-weight:bold;'/>\";"
    )
    return {
        "type": "SCRIPT",
        "x": x,
        "y": y,
        "dataPointXid": point_xid(row),
        "nameOverride": label,
        "settableOverride": True,
        "bkgdColorOverride": None,
        "displayControls": False,
        "script": script,
    }


def _latching_button(
    row: Register,
    x: int,
    y: int,
    off_label: str,
    on_label: str,
) -> Dict[str, Any]:
    return {
        "type": "BUTTON",
        "x": x,
        "y": y,
        "dataPointXid": point_xid(row),
        "nameOverride": row.name,
        "settableOverride": True,
        "bkgdColorOverride": None,
        "displayControls": False,
        "whenOffLabel": off_label,
        "whenOnLabel": on_label,
        "width": 180,
        "height": 34,
    }


def _view(
    xid: str,
    name: str,
    owner: str,
    components: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "xid": xid,
        "name": name,
        "user": owner,
        "anonymousAccess": "NONE",
        "viewComponents": components,
        "sharingUsers": [],
    }


def _overview_view(
    by_key: Mapping[Tuple[str, str, str], Register], owner: str
) -> Dict[str, Any]:
    components: List[Dict[str, Any]] = [
        _html_component(
            20,
            10,
            "<div style='font:700 25px Arial;color:#0f172a'>"
            "Thermal Plant — OpenPLC Gateway Overview</div>"
            "<div style='font:13px Arial;color:#475569'>"
            "Unit 1 · 250 ms · controls are available in each detail view"
            "</div>",
        )
    ]
    for index, device in enumerate(DEVICES):
        column = index % 4
        row_index = index // 4
        x = 20 + column * 305
        y = 80 + row_index * 340
        components.append(
            _html_component(
                x,
                y,
                "<div style='width:280px;border-bottom:2px solid #334155;"
                "font:700 17px Arial;color:#1e293b'>"
                + html.escape(DEVICE_LABELS[device])
                + "</div>",
            )
        )
        for status_index, status_name in enumerate(OVERVIEW_STATUS_NAMES):
            status = _lookup(
                by_key,
                device,
                status_name,
                ("DISCRETE", "INPUT"),
            )
            components.append(
                _simple_component(
                    status,
                    x,
                    y + 32 + status_index * 26,
                    status_name.replace("_", " "),
                )
            )
        for pv_index, pv_name in enumerate(KEY_PVS[device][:2]):
            pv = _lookup(by_key, device, pv_name, ("INPUT",))
            components.append(
                _simple_component(
                    pv,
                    x,
                    y
                    + 42
                    + len(OVERVIEW_STATUS_NAMES) * 26
                    + pv_index * 28,
                    pv_name.removesuffix("_HI").replace("_", " "),
                )
            )
    return _view(
        "GV_TPS_OVERVIEW",
        "Thermal Plant Overview",
        owner,
        components,
    )


def _detail_view(
    device: str,
    by_key: Mapping[Tuple[str, str, str], Register],
    owner: str,
) -> Dict[str, Any]:
    components: List[Dict[str, Any]] = [
        _html_component(
            20,
            10,
            "<div style='font:700 25px Arial;color:#0f172a'>"
            + html.escape(DEVICE_LABELS[device])
            + "</div><div style='font:13px Arial;color:#475569'>"
            "OpenPLC flat northbound image · write feedback is asynchronous"
            "</div>",
        ),
        _html_component(
            20,
            68,
            "<div style='font:700 18px Arial;color:#334155'>Status</div>",
        ),
    ]

    for index, (name, label) in enumerate(STATUS_POINTS):
        status = _lookup(by_key, device, name, ("DISCRETE", "INPUT"))
        components.append(
            _simple_component(
                status,
                20 + (index % 3) * 300,
                100 + (index // 3) * 30,
                label,
            )
        )

    status_rows = math.ceil(len(STATUS_POINTS) / 3)
    pv_start_y = 100 + status_rows * 30 + 55
    components.append(
        _html_component(
            20,
            pv_start_y - 30,
            "<div style='font:700 18px Arial;color:#334155'>"
            "Key Process Values</div>",
        )
    )
    for index, name in enumerate(KEY_PVS[device]):
        pv = _lookup(by_key, device, name, ("INPUT",))
        components.append(
            _simple_component(
                pv,
                20 + (index % 2) * 450,
                pv_start_y + (index // 2) * 30,
                name.removesuffix("_HI").replace("_", " "),
            )
        )

    pv_rows = math.ceil(len(KEY_PVS[device]) / 2)
    command_title_y = pv_start_y + pv_rows * 30 + 25
    components.append(
        _html_component(
            20,
            command_title_y,
            "<div style='font:700 18px Arial;color:#334155'>Commands</div>"
            "<div style='font:12px Arial;color:#64748b'>"
            "Pulse commands write TRUE only; the PLC/device one-shot clears them."
            "</div>",
        )
    )

    pulse_commands: List[Tuple[str, str]] = []
    if device != "feedwater_tank":
        pulse_commands.extend((("START", "START"), ("STOP", "STOP")))
    pulse_commands.extend(
        (("RESET_TRIP", "RESET TRIP"), ("ACK_ALARM", "ACK ALARM"))
    )
    if device == "generator":
        pulse_commands.extend(
            (("BREAKER_CLOSE", "BREAKER CLOSE"), ("BREAKER_OPEN", "BREAKER OPEN"))
        )

    pulse_y = command_title_y + 48
    for index, (name, label) in enumerate(pulse_commands):
        command = _lookup(by_key, device, name, ("COIL",))
        components.append(
            _pulse_component(
                command,
                20 + (index % 6) * 145,
                pulse_y + (index // 6) * 42,
                label,
            )
        )

    latch_y = pulse_y + math.ceil(len(pulse_commands) / 6) * 42 + 8
    emergency_stop = _lookup(
        by_key, device, "EMERGENCY_STOP", ("COIL",)
    )
    force_safe = _lookup(by_key, device, "FORCE_SAFE", ("COIL",))
    components.extend(
        (
            _latching_button(
                emergency_stop,
                20,
                latch_y,
                "ENGAGE E-STOP",
                "RELEASE E-STOP",
            ),
            _latching_button(
                force_safe,
                220,
                latch_y,
                "FORCE SAFE ON",
                "FORCE SAFE OFF",
            ),
        )
    )

    settings_title_y = latch_y + 70
    components.append(
        _html_component(
            20,
            settings_title_y,
            "<div style='font:700 18px Arial;color:#334155'>"
            "Effective Setpoints & Reset Handshake</div>"
            "<div style='font:12px Arial;color:#64748b'>"
            "RESET_TRIP requires RESET_KEY=42330 and a new COMMAND_SEQUENCE."
            "</div>",
        )
    )
    setting_names = list(MAJOR_SETTINGS[device]) + [
        "RESET_KEY",
        "COMMAND_SEQUENCE",
    ]
    for index, name in enumerate(setting_names):
        setting = _lookup(by_key, device, name, ("HOLDING",))
        label = name.replace("_", " ")
        if name == "RESET_KEY":
            label = "RESET KEY (42330 / 0xA55A)"
        elif name == "CONTROL_MODE":
            label = "CONTROL MODE (0..4)"
        components.append(
            _simple_component(
                setting,
                20 + (index % 3) * 360,
                settings_title_y + 50 + (index // 3) * 34,
                label,
                settable=True,
            )
        )

    return _view(
        f"GV_TPS_{device.upper()}",
        f"{DEVICE_LABELS[device]} Detail",
        owner,
        components,
    )


def build_views(
    registers: Sequence[Register], owner: str
) -> List[Dict[str, Any]]:
    by_key = {row.key: row for row in registers}
    views = [_overview_view(by_key, owner)]
    views.extend(_detail_view(device, by_key, owner) for device in DEVICES)
    return views


def _overview_point_rows(
    by_key: Mapping[Tuple[str, str, str], Register],
) -> List[Register]:
    rows: List[Register] = []
    for device in DEVICES:
        for name in OVERVIEW_STATUS_NAMES:
            rows.append(_lookup(by_key, device, name, ("DISCRETE", "INPUT")))
        for name in KEY_PVS[device][:2]:
            rows.append(_lookup(by_key, device, name, ("INPUT",)))
    return rows


def build_watchlists(
    registers: Sequence[Register], owner: str
) -> List[Dict[str, Any]]:
    by_key = {row.key: row for row in registers}
    watchlists: List[Dict[str, Any]] = [
        {
            "xid": "WL_TPS_OVERVIEW",
            "user": owner,
            "dataPoints": [
                point_xid(row) for row in _overview_point_rows(by_key)
            ],
            "sharingUsers": [],
            "name": "Thermal Plant Overview",
        }
    ]
    for device in DEVICES:
        watchlists.append(
            {
                "xid": f"WL_TPS_{device.upper()}",
                "user": owner,
                "dataPoints": [
                    point_xid(row) for row in registers if row.device == device
                ],
                "sharingUsers": [],
                "name": f"{DEVICE_LABELS[device]} — All Points",
            }
        )
    return watchlists


def build_export(
    registers: Sequence[Register], host: str, owner: str
) -> Dict[str, Any]:
    return {
        "graphicalViews": build_views(registers, owner),
        "dataSources": [build_data_source(host)],
        "dataPoints": [build_data_point(row) for row in registers],
        "watchLists": build_watchlists(registers, owner),
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"ScadaBR 1.2 schema check failed: {message}")


def _validate_xids(export: Mapping[str, Any]) -> None:
    seen = set()
    for root_key in ("dataSources", "dataPoints", "watchLists", "graphicalViews"):
        for item in export[root_key]:
            xid = item.get("xid")
            _require(isinstance(xid, str), f"{root_key} item has no string xid")
            _require(
                XID_PATTERN.fullmatch(xid) is not None,
                f"invalid or too-long XID {xid!r}",
            )
            _require(xid not in seen, f"duplicate XID {xid}")
            seen.add(xid)


def validate_export(
    export: Mapping[str, Any],
    registers: Sequence[Register],
    *,
    host: str,
    owner: str,
) -> None:
    """Static check against the ScadaBR 1.2 importer/VO contract.

    This mirrors the official ScadaBR 1.2 Java source fields used by
    ImportTask, ModbusIpDataSourceVO, ModbusPointLocatorVO, DataPointVO,
    View, WatchList, and the native view components.
    """

    _require(set(export) == ROOT_KEYS, f"root keys must be exactly {sorted(ROOT_KEYS)}")
    for key in ROOT_KEYS:
        _require(isinstance(export[key], list), f"{key} must be an array")
    _validate_xids(export)

    data_sources = export["dataSources"]
    _require(len(data_sources) == 1, "exactly one data source is required")
    data_source = data_sources[0]
    _require(data_source["xid"] == DATA_SOURCE_XID, "unexpected data source XID")
    _require(data_source["type"] == "MODBUS_IP", "data source type must be MODBUS_IP")
    _require(data_source["transportType"] == "TCP", "transportType must be TCP")
    _require(data_source["host"] == host, "data source host mismatch")
    _require(data_source["port"] == 502, "data source port must be 502")
    _require(data_source["updatePeriodType"] == "MILLISECONDS", "poll unit mismatch")
    _require(data_source["updatePeriods"] == 250, "poll interval must be 250 ms")
    _require(data_source["enabled"] is True, "data source must be enabled")

    expected_by_xid = {point_xid(row): row for row in registers}
    data_points = export["dataPoints"]
    _require(
        len(data_points) == len(registers),
        f"expected {len(registers)} data points, got {len(data_points)}",
    )
    _require(
        len(expected_by_xid) == len(registers),
        "deterministic point XIDs are not unique",
    )
    actual_by_xid = {point["xid"]: point for point in data_points}
    _require(set(actual_by_xid) == set(expected_by_xid), "data point XID set mismatch")

    allowed_ranges = set(RANGES.values())
    allowed_types = {
        "BINARY",
        "TWO_BYTE_INT_UNSIGNED",
        "TWO_BYTE_INT_SIGNED",
        "FOUR_BYTE_INT_UNSIGNED",
    }
    for xid, row in expected_by_xid.items():
        point = actual_by_xid[xid]
        locator = point.get("pointLocator")
        _require(isinstance(locator, dict), f"{xid} pointLocator must be an object")
        _require(point["dataSourceXid"] == DATA_SOURCE_XID, f"{xid} DS reference")
        _require(point["deviceName"] == row.device, f"{xid} deviceName mismatch")
        _require(point["name"] == row.name, f"{xid} point name mismatch")
        _require(
            point["engineeringUnits"] == row.unit,
            f"{xid} engineeringUnits does not preserve the CSV unit",
        )
        _require(locator["range"] in allowed_ranges, f"{xid} invalid range")
        _require(
            locator["range"] == RANGES[row.table],
            f"{xid} range does not match {row.table}",
        )
        _require(
            locator["modbusDataType"] in allowed_types,
            f"{xid} invalid Modbus data type",
        )
        _require(
            locator["modbusDataType"] == _modbus_data_type(row),
            f"{xid} Modbus data type mismatch",
        )
        _require(locator["offset"] == flat_offset(row), f"{xid} flat offset mismatch")
        _require(locator["slaveId"] == 1, f"{xid} slaveId must be 1")
        expected_settable = row.table in WRITABLE_TABLES and row.writable
        _require(
            locator["settableOverride"] is expected_settable,
            f"{xid} locator settableOverride mismatch",
        )
        expected_multiplier = 1.0 if row.table in BIT_TABLES else 1.0 / row.scale
        _require(
            math.isclose(
                locator["multiplier"],
                expected_multiplier,
                rel_tol=0.0,
                abs_tol=1e-15,
            ),
            f"{xid} multiplier mismatch",
        )
        if row.dtype == "u32":
            _require(
                locator["modbusDataType"] == "FOUR_BYTE_INT_UNSIGNED",
                f"{xid} u32 must be High Word First FOUR_BYTE_INT_UNSIGNED",
            )
        if row.dtype == "i16":
            _require(
                locator["modbusDataType"] == "TWO_BYTE_INT_SIGNED",
                f"{xid} i16 must be signed",
            )

    point_refs = set(actual_by_xid)
    watchlists = export["watchLists"]
    _require(len(watchlists) == 9, "expected overview plus eight device watchlists")
    by_key = {row.key: row for row in registers}
    expected_overview = [
        point_xid(row) for row in _overview_point_rows(by_key)
    ]
    _require(
        watchlists[0]["dataPoints"] == expected_overview,
        "overview watchlist points/order mismatch",
    )
    for index, watchlist in enumerate(watchlists):
        _require(watchlist["user"] == owner, f"watchlist {index} owner mismatch")
        _require(watchlist["sharingUsers"] == [], "watchlist sharingUsers must be empty")
        _require(
            all(ref in point_refs for ref in watchlist["dataPoints"]),
            f"watchlist {index} has missing point reference",
        )
    for index, device in enumerate(DEVICES, start=1):
        expected = [
            point_xid(row) for row in registers if row.device == device
        ]
        _require(
            watchlists[index]["dataPoints"] == expected,
            f"{device} watchlist does not contain every device point",
        )

    views = export["graphicalViews"]
    _require(len(views) == 9, "expected overview plus eight graphical views")
    allowed_components = {"HTML", "SIMPLE", "SCRIPT", "BUTTON"}
    xid_to_row = {point_xid(row): row for row in registers}
    for view in views:
        _require(view["user"] == owner, f"{view['xid']} owner mismatch")
        _require(view["anonymousAccess"] == "NONE", f"{view['xid']} must be private")
        _require(view["sharingUsers"] == [], f"{view['xid']} sharing must be empty")
        components = view.get("viewComponents")
        _require(isinstance(components, list), f"{view['xid']} components must be array")
        for component in components:
            component_type = component.get("type")
            _require(
                component_type in allowed_components,
                f"{view['xid']} invalid component type {component_type!r}",
            )
            _require(
                isinstance(component.get("x"), int)
                and isinstance(component.get("y"), int)
                and component["x"] >= 0
                and component["y"] >= 0,
                f"{view['xid']} component has invalid coordinates",
            )
            if component_type == "HTML":
                _require(
                    isinstance(component.get("content"), str),
                    f"{view['xid']} HTML content must be a string",
                )
                continue
            ref = component.get("dataPointXid")
            _require(ref in point_refs, f"{view['xid']} references missing point {ref}")
            _require(
                isinstance(component.get("settableOverride"), bool),
                f"{view['xid']} point component needs settableOverride",
            )
            if component["settableOverride"]:
                row = xid_to_row[ref]
                _require(
                    actual_by_xid[ref]["pointLocator"]["settableOverride"] is True,
                    f"{view['xid']} {row.device}.{row.name} lacks inner write permission",
                )
                _require(
                    row.table in WRITABLE_TABLES and row.writable,
                    f"{view['xid']} attempts to write read-only {row.device}.{row.name}",
                )
            if component_type == "SCRIPT":
                row = xid_to_row[ref]
                script = component.get("script", "")
                _require(row.pulse, f"{row.device}.{row.name} SCRIPT must be a pulse")
                _require(
                    "mango.view.setPoint(" in script and ",true)" in script,
                    f"{row.device}.{row.name} pulse must write TRUE",
                )
                _require(
                    ",false)" not in script,
                    f"{row.device}.{row.name} pulse must never write FALSE",
                )
            if component_type == "BUTTON":
                row = xid_to_row[ref]
                _require(
                    not row.pulse,
                    f"{row.device}.{row.name} pulse cannot use toggle BUTTON",
                )

    detail_views = {
        device: next(
            view for view in views if view["xid"] == f"GV_TPS_{device.upper()}"
        )
        for device in DEVICES
    }
    for device, view in detail_views.items():
        referenced_names = {
            xid_to_row[component["dataPointXid"]].name
            for component in view["viewComponents"]
            if "dataPointXid" in component
        }
        required = {
            "RESET_TRIP",
            "ACK_ALARM",
            "EMERGENCY_STOP",
            "FORCE_SAFE",
            "RESET_KEY",
            "COMMAND_SEQUENCE",
            *MAJOR_SETTINGS[device],
        }
        if device != "feedwater_tank":
            required.update(("START", "STOP"))
        _require(
            required <= referenced_names,
            f"{device} HMI missing controls {sorted(required - referenced_names)}",
        )
    tank_refs = {
        xid_to_row[component["dataPointXid"]].name
        for component in detail_views["feedwater_tank"]["viewComponents"]
        if "dataPointXid" in component
    }
    _require(
        "START" not in tank_refs and "STOP" not in tank_refs,
        "feedwater_tank HMI must not expose ineffective START/STOP",
    )
    generator_refs = {
        xid_to_row[component["dataPointXid"]].name
        for component in detail_views["generator"]["viewComponents"]
        if "dataPointXid" in component
    }
    _require(
        {"BREAKER_STATUS", "BREAKER_CLOSE", "BREAKER_OPEN"} <= generator_refs,
        "generator HMI must include breaker status/close/open",
    )


def render_json(export: Mapping[str, Any]) -> str:
    return json.dumps(export, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--register-map",
        type=Path,
        default=DEFAULT_REGISTER_MAP,
        help=f"register CSV (default: {DEFAULT_REGISTER_MAP})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"ScadaBR JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="OpenPLC Modbus/TCP host written into the data source",
    )
    parser.add_argument(
        "--owner",
        default="admin",
        help="existing ScadaBR username that owns views/watchlists",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and compare with the existing output without writing",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] = ()) -> int:
    args = parse_args(argv)
    source_rows = load_registers(args.register_map)
    registers = point_registers(source_rows)
    export = build_export(registers, args.host, args.owner)
    validate_export(export, registers, host=args.host, owner=args.owner)
    rendered = render_json(export)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    if args.check:
        try:
            existing = args.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            print(f"ERROR: generated output does not exist: {args.output}", file=sys.stderr)
            return 1
        if existing != rendered:
            print(
                "ERROR: generated output is stale; run without --check",
                file=sys.stderr,
            )
            return 1
        action = "checked"
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        action = "wrote"

    u32_count = sum(row.dtype == "u32" for row in registers)
    signed_count = sum(row.dtype == "i16" for row in registers)
    print(
        f"{action} {args.output}: {len(registers)} points "
        f"({u32_count} u32 composites, {signed_count} signed i16), "
        f"{len(export['watchLists'])} watchlists, "
        f"{len(export['graphicalViews'])} graphical views, "
        f"sha256={digest}"
    )
    print(
        "ScadaBR 1.2 static schema check: PASS "
        "(MODBUS_IP / DataPointVO / View / WatchList / native components)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
