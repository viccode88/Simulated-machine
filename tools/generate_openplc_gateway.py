#!/usr/bin/env python3
"""Generate the OpenPLC Editor v4 thermal-plant gateway project.

The generated project is source-only: the stale ``build/`` directory from the
reference v4.rar is deliberately not copied.  All southbound point names,
offsets, scaling metadata, writable flags, and pulse semantics come from
``docs/register-map.csv``.

The OpenPLC v4 Modbus remote-device schema used here was checked against the
official Editor and Runtime sources.  In particular, FC15 and FC16 use one
ioPoint per address and the exact UI type strings expected by the Editor.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = REPO_ROOT / "docs" / "register-map.csv"
DEFAULT_OUTPUT = REPO_ROOT / "integrations" / "openplc" / "thermal-plant-v4"

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
CONTAINER_HOSTS = {name: name.replace("_", "-") for name in DEVICE_ORDER}
HOST_PORTS = {name: 15021 + index for index, name in enumerate(DEVICE_ORDER)}

WORD_BLOCK = 64
BIT_BLOCK = 16
HOLDING_READBACK_BASE = 512
READ_CYCLE_MS = 250
WRITE_CYCLE_MS = 100
TASK_INTERVAL_MS = 20
PULSE_SCANS = 8  # 8 * 20 ms = approximately 160 ms
WATCHDOG_SCANS = 10  # 10 * 20 ms = 200 ms
ECHO_STALE_TICKS = 15  # 15 * 200 ms = 3 seconds
RESET_KEY = 0xA55A
OVERSPEED_RAW_RPM = 3150

CSV_FIELDS = (
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

NAMESPACE = uuid.UUID("16d493c1-9c75-5b80-a63b-95cf854f8792")

POINT_TYPES = {
    "2": "Digital Input (Discrete Input)",
    "3": "Analog Input (Holding Register)",
    "4": "Analog Input (Input Register)",
    "15": "Digital Output (Multiple Coils)",
    "16": "Analog Output (Multiple Registers)",
}


@dataclass(frozen=True)
class Register:
    device: str
    table: str
    doc_address: int
    offset: int
    name: str
    dtype: str
    unit: str
    scale: float
    minimum: float | None
    maximum: float | None
    writable: bool
    pulse: bool
    description: str


@dataclass(frozen=True)
class GroupSpec:
    function_code: str
    remote_offset: int
    length: int
    local_area: str
    local_start: int
    alias_table: str
    contract_table: str
    cycle_ms: int
    suffix: str


def _number(value: str) -> float | None:
    value = value.strip()
    return float(value) if value else None


def load_contract(path: Path) -> list[Register]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise ValueError(
                f"{path}: unexpected header {reader.fieldnames!r}; expected {CSV_FIELDS!r}"
            )
        rows = [
            Register(
                device=row["device"].strip(),
                table=row["table"].strip().upper(),
                doc_address=int(row["doc_address"]),
                offset=int(row["pdu_offset"]),
                name=row["name"].strip(),
                dtype=row["dtype"].strip(),
                unit=row["unit"].strip(),
                scale=_number(row["scale"]) or 1.0,
                minimum=_number(row["min"]),
                maximum=_number(row["max"]),
                writable=row["writable"].strip().lower() == "yes",
                pulse=row["pulse"].strip().lower() == "yes",
                description=row["description"].strip(),
            )
            for row in reader
        ]
    validate_contract(rows, path)
    return rows


def validate_contract(rows: Sequence[Register], path: Path) -> None:
    devices = tuple(dict.fromkeys(row.device for row in rows))
    if devices != DEVICE_ORDER:
        raise ValueError(f"{path}: device order {devices!r} != required {DEVICE_ORDER!r}")

    by_key: dict[tuple[str, str, int], Register] = {}
    by_name: set[tuple[str, str, str]] = set()
    for row in rows:
        if row.table not in {"COIL", "DISCRETE", "INPUT", "HOLDING"}:
            raise ValueError(f"{path}: unsupported table {row.table!r}")
        key = (row.device, row.table, row.offset)
        if key in by_key:
            raise ValueError(f"{path}: duplicate offset {key}")
        name_key = (row.device, row.table, row.name)
        if name_key in by_name:
            raise ValueError(f"{path}: duplicate name {name_key}")
        by_key[key] = row
        by_name.add(name_key)

    for device in DEVICE_ORDER:
        discrete = {r.offset for r in rows if r.device == device and r.table == "DISCRETE"}
        if discrete != set(range(16)):
            raise ValueError(f"{path}: {device} DISCRETE must contain offsets 0..15")
        inputs = {r.offset for r in rows if r.device == device and r.table == "INPUT"}
        if not inputs or min(inputs) < 0 or max(inputs) >= 50:
            raise ValueError(f"{path}: {device} INPUT must fit FC4 offset 0 length 50")
        coils = {r.offset for r in rows if r.device == device and r.table == "COIL"}
        expected_coils = set(range(8)) | ({9, 10} if device == "generator" else set())
        if coils != expected_coils:
            raise ValueError(
                f"{path}: {device} COIL offsets {sorted(coils)} != {sorted(expected_coils)}"
            )
        holdings = {r.offset for r in rows if r.device == device and r.table == "HOLDING"}
        required = set(range(14)) | set(range(19, 25))
        if not required <= holdings or not holdings <= (required | {29, 30}):
            raise ValueError(f"{path}: {device} HOLDING groups do not match the contract")
        for row in rows:
            if row.device != device:
                continue
            if row.table in {"COIL", "HOLDING"} and not row.writable:
                raise ValueError(f"{path}: output point is not writable: {row}")


def contract_index(rows: Sequence[Register]) -> dict[tuple[str, str, int], Register]:
    return {(row.device, row.table, row.offset): row for row in rows}


def slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    if not value or value[0].isdigit():
        value = f"p_{value}"
    return value


def alias(device: str, table: str, name: str) -> str:
    """Every alias has a globally unique device/table/name prefix."""
    return f"{slug(device)}__{slug(table)}__{slug(name)}"


def bit_location(area: str, bit_index: int) -> str:
    return f"%{area}{bit_index // 8}.{bit_index % 8}"


def word_location(area: str, word_index: int) -> str:
    return f"%{area}{word_index}"


def display_name(index: dict[tuple[str, str, int], Register], device: str,
                 table: str, offset: int) -> str:
    row = index.get((device, table, offset))
    return row.name if row else f"RESERVED_{offset:02d}"


def device_groups(device_index: int, device: str,
                  rows: Sequence[Register]) -> list[GroupSpec]:
    word_base = device_index * WORD_BLOCK
    bit_base = device_index * BIT_BLOCK
    groups = [
        GroupSpec("4", 0, 50, "IW", word_base, "input", "INPUT",
                  READ_CYCLE_MS, "input_0_49"),
        GroupSpec("2", 0, 16, "IX", bit_base, "discrete", "DISCRETE",
                  READ_CYCLE_MS, "discrete_0_15"),
        GroupSpec("3", 0, 32, "IW", HOLDING_READBACK_BASE + word_base,
                  "holding_readback", "HOLDING", READ_CYCLE_MS,
                  "holding_readback_0_31"),
        GroupSpec("15", 0, 8, "QX", bit_base, "coil", "COIL",
                  WRITE_CYCLE_MS, "coil_0_7"),
    ]
    if device == "generator":
        groups.append(
            GroupSpec("15", 9, 2, "QX", bit_base + 9, "coil", "COIL",
                      WRITE_CYCLE_MS, "coil_9_10")
        )
    groups.extend(
        [
            GroupSpec("16", 0, 14, "QW", word_base, "holding", "HOLDING",
                      WRITE_CYCLE_MS, "holding_0_13"),
            GroupSpec("16", 19, 6, "QW", word_base + 19, "holding", "HOLDING",
                      WRITE_CYCLE_MS, "holding_19_24"),
        ]
    )
    extra_offsets = sorted(
        row.offset
        for row in rows
        if row.device == device and row.table == "HOLDING" and row.offset >= 29
    )
    if extra_offsets:
        if extra_offsets != list(range(extra_offsets[0], extra_offsets[-1] + 1)):
            raise ValueError(f"{device}: device-specific holding offsets are not contiguous")
        groups.append(
            GroupSpec(
                "16",
                extra_offsets[0],
                len(extra_offsets),
                "QW",
                word_base + extra_offsets[0],
                "holding",
                "HOLDING",
                WRITE_CYCLE_MS,
                f"holding_{extra_offsets[0]}_{extra_offsets[-1]}",
            )
        )
    return groups


def make_io_group(device: str, spec: GroupSpec,
                  index: dict[tuple[str, str, int], Register]) -> dict:
    group_name = f"{device}_{spec.suffix}"
    points = []
    for point_index in range(spec.length):
        remote_offset = spec.remote_offset + point_index
        point_name = display_name(index, device, spec.contract_table, remote_offset)
        local_index = spec.local_start + point_index
        location = (
            bit_location(spec.local_area, local_index)
            if spec.local_area.endswith("X")
            else word_location(spec.local_area, local_index)
        )
        point_alias = alias(device, spec.alias_table, point_name)
        points.append(
            {
                "id": str(uuid.uuid5(NAMESPACE, f"{group_name}:point:{point_index}")),
                "name": point_alias,
                "type": POINT_TYPES[spec.function_code],
                "iecLocation": location,
                "alias": point_alias,
            }
        )
    return {
        "id": str(uuid.uuid5(NAMESPACE, f"{group_name}:group")),
        "name": group_name,
        "functionCode": spec.function_code,
        "cycleTime": spec.cycle_ms,
        "offset": str(spec.remote_offset),
        "length": spec.length,
        "errorHandling": "keep-last-value",
        "ioPoints": points,
    }


def make_remote(device_index: int, device: str, rows: Sequence[Register],
                host_mode: str) -> dict:
    host = CONTAINER_HOSTS[device] if host_mode == "container" else "127.0.0.1"
    port = 502 if host_mode == "container" else HOST_PORTS[device]
    index = contract_index(rows)
    return {
        "name": device,
        "protocol": "modbus-tcp",
        "modbusTcpConfig": {
            "host": host,
            "port": port,
            "slaveId": 1,
            "timeout": 1000,
            "ioGroups": [
                make_io_group(device, spec, index)
                for spec in device_groups(device_index, device, rows)
            ],
        },
    }


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def make_project_json() -> dict:
    # Source structure retained from v4.rar, with the example LD program
    # replaced by the generated source-only ST program.
    return {
        "meta": {"name": "thermal-plant-v4", "type": "plc-project"},
        "data": {
            "dataTypes": [],
            "pous": [],
            "configuration": {
                "resource": {
                    "tasks": [
                        {
                            "name": "task0",
                            "triggering": "Cyclic",
                            "interval": f"T#{TASK_INTERVAL_MS}ms",
                            "priority": 1,
                        }
                    ],
                    "instances": [
                        {"name": "instance0", "task": "task0", "program": "main"}
                    ],
                    "globalVariables": [],
                }
            },
            "libraries": [],
        },
    }


def make_configuration_json() -> dict:
    return {
        "deviceBoard": "OpenPLC Runtime v4",
        "communicationPort": "",
        "runtimeIpAddress": "127.0.0.1",
        "vendorScreenData": {},
        "vendorScreenDataByBoard": {
            "OpenPLC Simulator": {},
            "OpenPLC Runtime v4": {},
            "OpenPLC Runtime v3": {},
        },
        "selectedPlatformOptions": {},
    }


def make_server_json() -> dict:
    # Runtime v4 defaults expose 1024 %IW/%QW words and 8192 %IX/%QX bits,
    # which exactly covers this project's highest addresses.
    return {
        "name": "thermal_gateway",
        "protocol": "modbus-tcp",
        "modbusSlaveConfig": {
            "enabled": True,
            "networkInterface": "0.0.0.0",
            "port": 502,
        },
    }


def raw_minimum(row: Register) -> int:
    if row.minimum is None:
        return 0
    return max(0, min(0xFFFF, int(round(row.minimum * row.scale))))


def raw_maximum(row: Register) -> int:
    if row.maximum is None:
        return 0xFFFF
    return max(0, min(0xFFFF, int(round(row.maximum * row.scale))))


def safe_initial(row: Register, device_index: int) -> int:
    """Return a valid fail-safe initial raw value for every %QW point."""
    fixed = {
        "CONTROL_MODE": 3,  # REMOTE_AUTO; outputs/setpoints still start low.
        "COMMAND_SEQUENCE": 0,
        "WATCHDOG_COUNTER": 1,
        "RESET_KEY": 0,
        "SIMULATION_FLAGS": 0,
        "ACTIVE_CONTROLLER_ID": device_index + 1,
        "COMMAND_LEASE_TIME": 5,
        "OUTPUT_RATE_LIMIT": 1000,  # raw 10.00 %/s
        "RESERVED": 0,
        "OUTPUT_HIGH_LIMIT": 10000,  # raw 100.00 %
        "OUTPUT_LOW_LIMIT": 0,
        "CONTROLLER_SCAN_TIME": 200,
    }
    value = fixed.get(row.name, raw_minimum(row))
    return max(raw_minimum(row), min(raw_maximum(row), value))


def all_remotes(rows: Sequence[Register], host_mode: str) -> list[dict]:
    return [
        make_remote(device_index, device, rows, host_mode)
        for device_index, device in enumerate(DEVICE_ORDER)
    ]


def declarations(remotes: Sequence[dict], rows: Sequence[Register]) -> list[str]:
    index = contract_index(rows)
    output = []
    seen_aliases: set[str] = set()
    seen_locations: set[str] = set()
    for remote in remotes:
        device = remote["name"]
        device_index = DEVICE_ORDER.index(device)
        for group in remote["modbusTcpConfig"]["ioGroups"]:
            fc = group["functionCode"]
            group_offset = int(group["offset"])
            for point_index, point in enumerate(group["ioPoints"]):
                point_alias = point["alias"]
                location = point["iecLocation"]
                if point_alias in seen_aliases:
                    raise ValueError(f"duplicate ST alias {point_alias}")
                if location in seen_locations:
                    raise ValueError(f"duplicate ST location {location}")
                seen_aliases.add(point_alias)
                seen_locations.add(location)
                if location.startswith(("%IX", "%QX")):
                    initial = "FALSE"
                    iec_type = "BOOL"
                else:
                    initial_value = 0
                    if fc == "16":
                        remote_offset = group_offset + point_index
                        row = index[(device, "HOLDING", remote_offset)]
                        initial_value = safe_initial(row, device_index)
                    initial = str(initial_value)
                    iec_type = "UINT"
                output.append(
                    f"    {point_alias} AT {location} : {iec_type} := {initial};"
                )
    return output


def safe_output_initialization(rows: Sequence[Register]) -> list[str]:
    """Emit executable first-scan assignments for every southbound %QW.

    OpenPLC Runtime v4 can start located output variables with a zeroed process
    image even when their ST declarations contain ``:=`` initializers. Values
    whose contract minimum is above zero would then be rejected by the device
    with Modbus exception 03.
    """
    output = [
        "  (* Runtime v4 may zero located %QW variables after declaration setup.",
        "     Copy every contract-safe value into the output image on first scan. *)",
        "  IF NOT i_outputs_initialized THEN",
    ]
    for device_index, device in enumerate(DEVICE_ORDER):
        device_rows = sorted(
            (
                row
                for row in rows
                if row.device == device and row.table == "HOLDING"
            ),
            key=lambda row: row.offset,
        )
        for row in device_rows:
            output.append(
                f"    {alias(device, 'holding', row.name)} := "
                f"{safe_initial(row, device_index)};"
            )
    output.extend(
        [
            "    i_outputs_initialized := TRUE;",
            "  END_IF;",
            "",
        ]
    )
    return output


def _pulse_state_name(device: str, name: str, suffix: str) -> str:
    return f"i_{slug(device)}_{slug(name)}_{suffix}"


def make_main_st(rows: Sequence[Register], remotes: Sequence[dict]) -> str:
    pulse_rows = [
        row for row in rows if row.table == "COIL" and row.pulse
    ]
    trip_sources = (
        "turbine",
        "boiler",
        "condenser",
        "feedwater_pump",
        "condensate_pump",
        "steam_valve",
    )

    lines = [
        "(* Generated by tools/generate_openplc_gateway.py.",
        "   Raw Modbus values are intentionally retained; engineering_value = raw / scale.",
        "   The cyclic task interval is 20 ms. *)",
        "PROGRAM main",
        "  VAR",
        "    i_outputs_initialized : BOOL := FALSE;",
        "    i_watchdog_scan_count : UINT := 0;",
        "    i_watchdog_tick : BOOL := FALSE;",
    ]
    for device in DEVICE_ORDER:
        lines.extend(
            [
                f"    i_{device}_last_echo : UINT := 0;",
                f"    i_{device}_echo_stale_ticks : UINT := 0;",
                f"    i_{device}_comm_lost : BOOL := FALSE;",
            ]
        )
    for row in pulse_rows:
        lines.extend(
            [
                f"    {_pulse_state_name(row.device, row.name, 'active')} : BOOL := FALSE;",
                f"    {_pulse_state_name(row.device, row.name, 'scans')} : UINT := 0;",
            ]
        )
    for device in trip_sources:
        lines.append(f"    i_{device}_trip_prev : BOOL := FALSE;")
    lines.extend(
        [
            "",
            *declarations(remotes, rows),
            "  END_VAR",
            "",
            *safe_output_initialization(rows),
            "  (* 200 ms non-zero watchdog for every device. *)",
            "  i_watchdog_tick := FALSE;",
            "  i_watchdog_scan_count := i_watchdog_scan_count + 1;",
            f"  IF i_watchdog_scan_count >= {WATCHDOG_SCANS} THEN",
            "    i_watchdog_scan_count := 0;",
            "    i_watchdog_tick := TRUE;",
        ]
    )
    for device in DEVICE_ORDER:
        wd = alias(device, "holding", "WATCHDOG_COUNTER")
        lines.extend(
            [
                f"    IF {wd} >= 65535 THEN",
                f"      {wd} := 1;",
                "    ELSE",
                f"      {wd} := {wd} + 1;",
                "    END_IF;",
            ]
        )
    lines.extend(["  END_IF;", ""])

    lines.extend(
        [
            "  (* Echo may lag the current counter because FC4 polls every 250 ms.",
            "     Progress or equality proves the link; an unchanged mismatch for",
            "     15 watchdog ticks (3 s) is a communication failure. *)",
            "  IF i_watchdog_tick THEN",
        ]
    )
    for device in DEVICE_ORDER:
        echo = alias(device, "input", "WATCHDOG_ECHO")
        wd = alias(device, "holding", "WATCHDOG_COUNTER")
        lines.extend(
            [
                f"    IF ({echo} = {wd}) OR ({echo} <> i_{device}_last_echo) THEN",
                f"      i_{device}_last_echo := {echo};",
                f"      i_{device}_echo_stale_ticks := 0;",
                "    ELSE",
                f"      IF i_{device}_echo_stale_ticks < 65535 THEN",
                f"        i_{device}_echo_stale_ticks := i_{device}_echo_stale_ticks + 1;",
                "      END_IF;",
                "    END_IF;",
                f"    i_{device}_comm_lost := i_{device}_echo_stale_ticks >= {ECHO_STALE_TICKS};",
            ]
        )
    lines.extend(["  END_IF;", ""])

    # Device-side policies are also implemented by the simulator.  Repeating
    # these actions in the PLC provides a second, explicit safety layer when
    # reads work but command echo stops progressing.
    lines.extend(
        [
            "  (* Communication fail-safe matrix from docs/plc-integration.md. *)",
            "  IF i_condenser_comm_lost THEN",
            f"    {alias('condenser', 'holding', 'CONTROL_MODE')} := 1;",
            "  END_IF;",
            "  IF i_condensate_pump_comm_lost THEN",
            f"    {alias('condensate_pump', 'holding', 'CONTROL_MODE')} := 1;",
            "  END_IF;",
            "  IF i_boiler_comm_lost THEN",
            f"    {alias('boiler', 'holding', 'MANUAL_OUTPUT')} := 0;",
            "  END_IF;",
            "  IF i_turbine_comm_lost THEN",
            f"    {alias('turbine', 'holding', 'MANUAL_OUTPUT')} := 0;",
            "  END_IF;",
            "  IF i_steam_valve_comm_lost THEN",
            f"    {alias('steam_valve', 'holding', 'MANUAL_OUTPUT')} := 0;",
            "  END_IF;",
            "  (* feedwater_pump/feedwater_tank/generator intentionally HOLD_LAST. *)",
            "",
            "  (* Pulse outputs are stretched to about 160 ms then cleared.",
            "     EMERGENCY_STOP and FORCE_SAFE are deliberately not included. *)",
        ]
    )
    for row in pulse_rows:
        command = alias(row.device, "coil", row.name)
        active = _pulse_state_name(row.device, row.name, "active")
        scans = _pulse_state_name(row.device, row.name, "scans")
        sequence = alias(row.device, "holding", "COMMAND_SEQUENCE")
        reset_key = alias(row.device, "holding", "RESET_KEY")
        lines.extend(
            [
                f"  IF {command} AND NOT {active} THEN",
                f"    {active} := TRUE;",
                f"    {scans} := 0;",
            ]
        )
        if row.name == "RESET_TRIP":
            lines.extend(
                [
                    f"    IF {sequence} >= 65535 THEN",
                    f"      {sequence} := 1;",
                    "    ELSE",
                    f"      {sequence} := {sequence} + 1;",
                    "    END_IF;",
                    f"    {reset_key} := {RESET_KEY};",
                ]
            )
        lines.extend(
            [
                "  END_IF;",
                f"  IF {active} THEN",
                f"    {scans} := {scans} + 1;",
                f"    IF {scans} >= {PULSE_SCANS} THEN",
                f"      {command} := FALSE;",
                f"      {active} := FALSE;",
                f"      {scans} := 0;",
            ]
        )
        if row.name == "RESET_TRIP":
            lines.append(f"      {reset_key} := 0;")
        lines.extend(["    END_IF;", "  END_IF;"])
    lines.append("")

    def di(device: str, name: str) -> str:
        by_key_offset(rows, device, "DISCRETE", name)
        return alias(device, "discrete", name)

    # Same rising-edge trip matrix as examples/external_plc.py.
    trip_actions: dict[str, list[str]] = {
        "turbine": [
            f"{alias('generator', 'coil', 'BREAKER_OPEN')} := TRUE;",
            f"{alias('steam_valve', 'holding', 'MANUAL_OUTPUT')} := 0;",
            f"{alias('generator', 'holding', 'PRIMARY_SETPOINT')} := 0;",
        ],
        "boiler": [
            f"{alias('boiler', 'holding', 'MANUAL_OUTPUT')} := 0;",
            f"{alias('steam_valve', 'holding', 'MANUAL_OUTPUT')} := 0;",
        ],
        "condenser": [
            f"{alias('generator', 'holding', 'PRIMARY_SETPOINT')} := 0;",
        ],
        "feedwater_pump": [
            f"{alias('boiler', 'holding', 'MANUAL_OUTPUT')} := 0;",
        ],
        "condensate_pump": [
            # 20.00% with scale 100.
            f"{alias('feedwater_pump', 'holding', 'MANUAL_OUTPUT')} := 2000;",
        ],
        "steam_valve": [
            f"{alias('boiler', 'holding', 'MANUAL_OUTPUT')} := 0;",
        ],
    }
    lines.append("  (* Rising-edge plant trip matrix, matching examples/external_plc.py. *)")
    for source, actions in trip_actions.items():
        tripped = di(source, "TRIPPED")
        lines.append(f"  IF {tripped} AND NOT i_{source}_trip_prev THEN")
        lines.extend(f"    {action}" for action in actions)
        lines.extend(
            [
                "  END_IF;",
                f"  i_{source}_trip_prev := {tripped};",
            ]
        )
    lines.extend(
        [
            "",
            "  (* Overspeed always closes the main steam valve, regardless of HMI demand. *)",
            (
                f"  IF ({alias('turbine', 'input', 'SPEED_RPM')} > "
                f"{OVERSPEED_RAW_RPM}) OR {di('turbine', 'TRIPPED')} THEN"
            ),
            f"    {alias('steam_valve', 'holding', 'MANUAL_OUTPUT')} := 0;",
            "  END_IF;",
            "END_PROGRAM",
            "",
        ]
    )
    return "\n".join(lines)


def by_key_offset(rows: Sequence[Register], device: str, table: str, name: str) -> int:
    matches = [
        row.offset
        for row in rows
        if row.device == device and row.table == table and row.name == name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {device}/{table}/{name}, got {matches}")
    return matches[0]


def make_northbound_csv(rows: Sequence[Register]) -> str:
    fields = (
        "device",
        "source_table",
        "name",
        "doc_address",
        "remote_pdu_offset",
        "local_iec",
        "northbound_table",
        "northbound_pdu_offset",
        "dtype",
        "unit",
        "scale",
        "writable",
        "pulse",
        "description",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for device_index, device in enumerate(DEVICE_ORDER):
        word_base = device_index * WORD_BLOCK
        bit_base = device_index * BIT_BLOCK
        for row in (candidate for candidate in rows if candidate.device == device):
            if row.table == "COIL":
                local_iec = bit_location("QX", bit_base + row.offset)
                north_table = "COIL"
                north_offset = bit_base + row.offset
            elif row.table == "DISCRETE":
                local_iec = bit_location("IX", bit_base + row.offset)
                north_table = "DISCRETE"
                north_offset = bit_base + row.offset
            elif row.table == "INPUT":
                local_iec = word_location("IW", word_base + row.offset)
                north_table = "INPUT"
                north_offset = word_base + row.offset
            else:
                local_iec = word_location("QW", word_base + row.offset)
                north_table = "HOLDING"
                north_offset = word_base + row.offset
            writer.writerow(
                {
                    "device": row.device,
                    "source_table": row.table,
                    "name": row.name,
                    "doc_address": row.doc_address,
                    "remote_pdu_offset": row.offset,
                    "local_iec": local_iec,
                    "northbound_table": north_table,
                    "northbound_pdu_offset": north_offset,
                    "dtype": row.dtype,
                    "unit": row.unit,
                    "scale": format(row.scale, "g"),
                    "writable": "yes" if row.writable else "no",
                    "pulse": "yes" if row.pulse else "no",
                    "description": row.description,
                }
            )
            if row.table == "HOLDING":
                # FC3 southbound readback is exposed through the northbound
                # Input Register table so HMI logic can verify accepted values.
                readback_offset = HOLDING_READBACK_BASE + word_base + row.offset
                writer.writerow(
                    {
                        "device": row.device,
                        "source_table": "HOLDING_READBACK",
                        "name": row.name,
                        "doc_address": row.doc_address,
                        "remote_pdu_offset": row.offset,
                        "local_iec": word_location("IW", readback_offset),
                        "northbound_table": "INPUT",
                        "northbound_pdu_offset": readback_offset,
                        "dtype": row.dtype,
                        "unit": row.unit,
                        "scale": format(row.scale, "g"),
                        "writable": "no",
                        "pulse": "no",
                        "description": f"FC3 readback: {row.description}".rstrip(),
                    }
                )
    return stream.getvalue()


def make_readme(host_mode: str) -> str:
    host_rows = "\n".join(
        f"| `{device}` | `{CONTAINER_HOSTS[device]}:502` | "
        f"`127.0.0.1:{HOST_PORTS[device]}` |"
        for device in DEVICE_ORDER
    )
    return f"""# Thermal Plant OpenPLC v4 gateway

This is a source-only OpenPLC Editor v4 project generated from
`docs/register-map.csv` and the source layout of the supplied `v4.rar`.
The archive's stale `build/` output is intentionally excluded.

Generated southbound mode: **{host_mode}**.

## Generate and verify

From the simulator repository root:

```sh
python3 tools/generate_openplc_gateway.py
python3 tools/generate_openplc_gateway.py --check
```

Host mode is the default because this repository does not define an OpenPLC
Compose service on `control_net`, and it matches the supplied `v4.rar`.
Use `--host-mode container` only when the OpenPLC Runtime has explicitly been
attached to `control_net`; that regenerates the remotes with service names and
port 502.

| Device | Container mode | Host mode |
| --- | --- | --- |
{host_rows}

Every device uses Modbus TCP Unit ID 1.  FC4, FC2, and FC3 reads run every
{READ_CYCLE_MS} ms.  FC15/FC16 writes run every {WRITE_CYCLE_MS} ms and therefore
continually refresh the controller lease.

## Flat northbound layout

The OpenPLC Modbus server listens on **0.0.0.0:502**.  SCADA/HMI clients connect
to the OpenPLC Runtime, not directly to the devices.  For device index `i` in
the fixed order below:

```text
word_base = 64 * i
bit_base  = 16 * i
```

| Northbound table | Local image | Content |
| --- | --- | --- |
| FC4 Input Registers | `%IW[word_base + 0..49]` | device FC4 process/diagnostic inputs |
| FC2 Discrete Inputs | `%IX[bit_base + 0..15]` | device FC2 status bits |
| FC4 Input Registers | `%IW[512 + word_base + 0..31]` | device FC3 holding readback |
| FC1/5/15 Coils | `%QX[bit_base + offset]` | device FC15 command coils |
| FC3/6/16 Holding Registers | `%QW[word_base + offset]` | device FC16 commands/setpoints |

Fixed device order:

```text
0 condenser
1 condensate_pump
2 feedwater_tank
3 feedwater_pump
4 boiler
5 steam_valve
6 turbine
7 generator
```

`northbound-map.csv` is the exact HMI import/reference map.  Its offsets are
zero-based PDU offsets.  Do not send the 3xxxx/4xxxx documentation address on
the wire.

## Values and HMI behavior

Values remain raw 16-bit Modbus words.  Decode with:

```text
engineering_value = raw / scale
```

`i16` uses two's-complement; `u32` is high-word first.  The CSV contains units,
types, limits, writable flags, pulse flags, and descriptions for HMI widgets.

The ST program explicitly copies every contract-safe value into `%QW` on its
first scan (located-variable declaration initializers alone are not reliable
with Runtime v4), advances each non-zero watchdog every 200 ms, and checks that
each FC4 watchdog echo continues to progress.  A stale mismatch for 3 seconds
applies the documented communication fail-safe policy.  It also implements the
same rising-edge trip matrix as `examples/external_plc.py` and closes the main
steam valve above {OVERSPEED_RAW_RPM} RPM.

HMI writes to pulse coils (`START`, `STOP`, `RESET_TRIP`, `ACK_ALARM`,
`TRIP_TEST`, `CLEAR_TOTALIZER`, plus generator breaker commands) are held for
approximately {PULSE_SCANS * TASK_INTERVAL_MS} ms and then cleared.  The
latched `EMERGENCY_STOP` and `FORCE_SAFE` coils are never auto-cleared.
On a `RESET_TRIP` rising edge the PLC increments the command sequence (skipping
zero) and applies reset key `0xA55A` for the pulse window.

## Safety notes

- Keep the OpenPLC cyclic task at {TASK_INTERVAL_MS} ms unless pulse and watchdog
  scan constants are reviewed together.
- OpenPLC Runtime v4 serializes remote groups.  Cycle time is a target, not a
  hard real-time guarantee.
- Modbus TCP has no authentication.  Restrict port 502 to the control/HMI
  network.
- `EMERGENCY_STOP` and `FORCE_SAFE` are maintained commands.  The operator must
  explicitly write `false` after the plant is safe and reset authorization is
  established.
"""


def build_files(rows: Sequence[Register], host_mode: str) -> dict[Path, str]:
    remotes = all_remotes(rows, host_mode)
    files: dict[Path, str] = {
        Path("project.json"): json_text(make_project_json()),
        Path("devices/configuration.json"): json_text(make_configuration_json()),
        Path("devices/pin-mapping.json"): json_text({}),
        Path("devices/servers/thermal_gateway.json"): json_text(make_server_json()),
        Path("pous/programs/main.st"): make_main_st(rows, remotes),
        Path("northbound-map.csv"): make_northbound_csv(rows),
        Path("README.md"): make_readme(host_mode),
    }
    for remote in remotes:
        files[Path("devices/remote") / f"{remote['name']}.json"] = json_text(remote)
    validate_generated(files, rows)
    return files


def validate_generated(files: dict[Path, str], rows: Sequence[Register]) -> None:
    expected_paths = {
        Path("project.json"),
        Path("devices/configuration.json"),
        Path("devices/pin-mapping.json"),
        Path("devices/servers/thermal_gateway.json"),
        Path("pous/programs/main.st"),
        Path("northbound-map.csv"),
        Path("README.md"),
        *{
            Path("devices/remote") / f"{device}.json"
            for device in DEVICE_ORDER
        },
    }
    if set(files) != expected_paths:
        raise ValueError(f"generated paths differ: {set(files) ^ expected_paths}")
    if any(part == "build" for path in files for part in path.parts):
        raise ValueError("stale build output must never be generated")

    aliases: set[str] = set()
    ids: set[str] = set()
    locations: set[str] = set()
    row_index = contract_index(rows)
    for device_index, device in enumerate(DEVICE_ORDER):
        path = Path("devices/remote") / f"{device}.json"
        remote = json.loads(files[path])
        config = remote["modbusTcpConfig"]
        groups = config["ioGroups"]
        expected_specs = device_groups(device_index, device, rows)
        if len(groups) != len(expected_specs):
            raise ValueError(f"{device}: group count mismatch")
        for group, spec in zip(groups, expected_specs):
            checks = {
                "functionCode": spec.function_code,
                "cycleTime": spec.cycle_ms,
                "offset": str(spec.remote_offset),
                "length": spec.length,
            }
            for key, expected in checks.items():
                if group[key] != expected:
                    raise ValueError(f"{device}/{group['name']}: {key} != {expected}")
            if len(group["ioPoints"]) != spec.length:
                raise ValueError(f"{device}/{group['name']}: ioPoints length mismatch")
            if group["id"] in ids:
                raise ValueError(f"duplicate group id {group['id']}")
            ids.add(group["id"])
            for point_index, point in enumerate(group["ioPoints"]):
                if point["type"] != POINT_TYPES[spec.function_code]:
                    raise ValueError(f"{device}/{point['name']}: type mismatch")
                remote_offset = spec.remote_offset + point_index
                point_name = display_name(
                    row_index, device, spec.contract_table, remote_offset
                )
                expected_alias = alias(device, spec.alias_table, point_name)
                local_index = spec.local_start + point_index
                expected_location = (
                    bit_location(spec.local_area, local_index)
                    if spec.local_area.endswith("X")
                    else word_location(spec.local_area, local_index)
                )
                if point["alias"] != expected_alias or point["name"] != expected_alias:
                    raise ValueError(
                        f"{device}/{group['name']}/{point_index}: alias/name mismatch"
                    )
                if point["iecLocation"] != expected_location:
                    raise ValueError(
                        f"{device}/{group['name']}/{point_index}: "
                        f"{point['iecLocation']} != {expected_location}"
                    )
                if point["alias"] in aliases:
                    raise ValueError(f"duplicate alias {point['alias']}")
                if point["id"] in ids:
                    raise ValueError(f"duplicate point id {point['id']}")
                if point["iecLocation"] in locations:
                    raise ValueError(f"duplicate IEC location {point['iecLocation']}")
                if not point["alias"].startswith(f"{device}__{spec.alias_table}__"):
                    raise ValueError(f"alias lacks device/table/name prefix: {point['alias']}")
                aliases.add(point["alias"])
                ids.add(point["id"])
                locations.add(point["iecLocation"])

    server = json.loads(files[Path("devices/servers/thermal_gateway.json")])
    server_config = server["modbusSlaveConfig"]
    if server_config != {
        "enabled": True,
        "networkInterface": "0.0.0.0",
        "port": 502,
    }:
        raise ValueError("northbound server must remain enabled on 0.0.0.0:502")

    st = files[Path("pous/programs/main.st")]
    if not st.startswith("(* Generated") or not st.rstrip().endswith("END_PROGRAM"):
        raise ValueError("main.st is not a complete generated PROGRAM")
    if len(re.findall(r"\bIF\b", st)) != len(re.findall(r"\bEND_IF\b", st)):
        raise ValueError("main.st IF/END_IF count mismatch")
    if f"> {OVERSPEED_RAW_RPM}" not in st or str(RESET_KEY) not in st:
        raise ValueError("main.st is missing overspeed/reset safety constants")
    pulse_width_ms = PULSE_SCANS * TASK_INTERVAL_MS
    if pulse_width_ms <= WRITE_CYCLE_MS:
        raise ValueError(
            f"pulse width {pulse_width_ms} ms must exceed FC15 cycle {WRITE_CYCLE_MS} ms"
        )
    for point_alias in aliases:
        if f"    {point_alias} AT %" not in st:
            raise ValueError(f"main.st does not declare remote alias {point_alias}")
    for row in rows:
        if row.table != "HOLDING":
            continue
        device_index = DEVICE_ORDER.index(row.device)
        location = word_location("QW", device_index * WORD_BLOCK + row.offset)
        expected_initial = safe_initial(row, device_index)
        pattern = (
            rf"\b{re.escape(alias(row.device, 'holding', row.name))} "
            rf"AT {re.escape(location)} : UINT := {expected_initial};"
        )
        if not re.search(pattern, st):
            raise ValueError(f"main.st lacks safe QW initializer for {row.device}/{row.name}")
    init_start = "  IF NOT i_outputs_initialized THEN\n"
    init_done = "    i_outputs_initialized := TRUE;\n"
    if st.count(init_start) != 1 or st.count(init_done) != 1:
        raise ValueError(
            "main.st must contain one executable first-scan QW initialization"
        )
    init_block = st.split(init_start, 1)[1].split(init_done, 1)[0]
    if st.index(init_start) > st.index("  (* 200 ms non-zero watchdog"):
        raise ValueError("first-scan QW initialization must run before watchdog logic")
    for row in rows:
        if row.table != "HOLDING":
            continue
        device_index = DEVICE_ORDER.index(row.device)
        expected_initial = safe_initial(row, device_index)
        assignment = (
            f"    {alias(row.device, 'holding', row.name)} := {expected_initial};"
        )
        if init_block.count(assignment) != 1:
            raise ValueError(
                "main.st lacks executable safe QW initialization for "
                f"{row.device}/{row.name}"
            )
    for row in rows:
        if row.table != "COIL":
            continue
        command = alias(row.device, "coil", row.name)
        if row.pulse:
            scans = _pulse_state_name(row.device, row.name, "scans")
            increment = f"{scans} := {scans} + 1;"
            clear = f"{command} := FALSE;"
            if st.count(increment) != 1:
                raise ValueError(
                    f"{row.device}/{row.name}: pulse scan must increment exactly once"
                )
            if st.count(clear) != 1:
                raise ValueError(
                    f"{row.device}/{row.name}: pulse command must clear exactly once"
                )
            if row.name == "RESET_TRIP":
                sequence = alias(row.device, "holding", "COMMAND_SEQUENCE")
                reset_key = alias(row.device, "holding", "RESET_KEY")
                if st.count(f"{sequence} := {sequence} + 1;") != 1:
                    raise ValueError(
                        f"{row.device}: reset sequence must increment exactly once"
                    )
                if st.count(f"{reset_key} := {RESET_KEY};") != 1:
                    raise ValueError(
                        f"{row.device}: reset key must be armed exactly once"
                    )
        else:
            forbidden = f"{alias(row.device, 'coil', row.name)} := FALSE;"
            if forbidden in st:
                raise ValueError(f"latched safety coil is auto-cleared: {forbidden}")

    north_rows = list(csv.DictReader(io.StringIO(files[Path("northbound-map.csv")])))
    expected_north_rows = len(rows) + sum(row.table == "HOLDING" for row in rows)
    if len(north_rows) != expected_north_rows:
        raise ValueError(
            f"northbound map has {len(north_rows)} rows; expected {expected_north_rows}"
        )
    north_keys = {
        (row["northbound_table"], int(row["northbound_pdu_offset"]))
        for row in north_rows
    }
    if len(north_keys) != len(north_rows):
        raise ValueError("northbound table/offset collisions found")
    north_index = {
        (row["device"], row["source_table"], row["name"]): row
        for row in north_rows
    }
    if len(north_index) != len(north_rows):
        raise ValueError("northbound device/source/name collisions found")
    for row in rows:
        device_index = DEVICE_ORDER.index(row.device)
        word_base = device_index * WORD_BLOCK
        bit_base = device_index * BIT_BLOCK
        mapped = north_index[(row.device, row.table, row.name)]
        if row.table == "COIL":
            expected_table = "COIL"
            expected_offset = bit_base + row.offset
            expected_iec = bit_location("QX", expected_offset)
        elif row.table == "DISCRETE":
            expected_table = "DISCRETE"
            expected_offset = bit_base + row.offset
            expected_iec = bit_location("IX", expected_offset)
        elif row.table == "INPUT":
            expected_table = "INPUT"
            expected_offset = word_base + row.offset
            expected_iec = word_location("IW", expected_offset)
        else:
            expected_table = "HOLDING"
            expected_offset = word_base + row.offset
            expected_iec = word_location("QW", expected_offset)
        if (
            mapped["northbound_table"] != expected_table
            or int(mapped["northbound_pdu_offset"]) != expected_offset
            or mapped["local_iec"] != expected_iec
        ):
            raise ValueError(f"northbound layout mismatch for {row.device}/{row.name}")
        if row.table == "HOLDING":
            readback = north_index[(row.device, "HOLDING_READBACK", row.name)]
            readback_offset = HOLDING_READBACK_BASE + word_base + row.offset
            if (
                readback["northbound_table"] != "INPUT"
                or int(readback["northbound_pdu_offset"]) != readback_offset
                or readback["local_iec"] != word_location("IW", readback_offset)
            ):
                raise ValueError(
                    f"northbound holding readback mismatch for {row.device}/{row.name}"
                )


def write_files(output: Path, files: dict[Path, str]) -> tuple[int, int]:
    written = 0
    unchanged = 0
    for relative, content in sorted(files.items(), key=lambda item: str(item[0])):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_text(encoding="utf-8") == content:
            unchanged += 1
            continue
        target.write_text(content, encoding="utf-8", newline="\n")
        written += 1
    return written, unchanged


def check_files(output: Path, files: dict[Path, str]) -> list[str]:
    problems = []
    for relative, content in sorted(files.items(), key=lambda item: str(item[0])):
        target = output / relative
        if not target.exists():
            problems.append(f"missing: {relative}")
        elif target.read_text(encoding="utf-8") != content:
            problems.append(f"out of date: {relative}")
    build_dir = output / "build"
    if build_dir.exists():
        problems.append("stale build directory exists")
    if output.exists():
        expected = {output / relative for relative in files}
        for target in sorted(path for path in output.rglob("*") if path.is_file()):
            if target not in expected:
                problems.append(f"unexpected generated-tree file: {target.relative_to(output)}")
    return problems


def summary(files: dict[Path, str], rows: Sequence[Register]) -> str:
    remote_paths = [
        path for path in files if path.parts[:2] == ("devices", "remote")
    ]
    group_count = 0
    point_count = 0
    for path in remote_paths:
        remote = json.loads(files[path])
        groups = remote["modbusTcpConfig"]["ioGroups"]
        group_count += len(groups)
        point_count += sum(len(group["ioPoints"]) for group in groups)
    north_rows = len(
        list(csv.DictReader(io.StringIO(files[Path("northbound-map.csv")])))
    )
    st_lines = len(files[Path("pous/programs/main.st")].splitlines())
    return (
        f"{len(remote_paths)} remotes, {group_count} groups, {point_count} ioPoints, "
        f"{len(rows)} contract rows, {north_rows} northbound rows, {st_lines} ST lines"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the source-only OpenPLC v4 thermal-plant gateway"
    )
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--host-mode",
        choices=("container", "host"),
        default="host",
        help="container: service names + 502; host: 127.0.0.1 + 15021..15028",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and compare generated content without writing",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rows = load_contract(args.map.resolve())
        files = build_files(rows, args.host_mode)
        if args.check:
            problems = check_files(args.output.resolve(), files)
            if problems:
                for problem in problems:
                    print(f"[FAIL] {problem}", file=sys.stderr)
                return 1
            print(f"[PASS] deterministic/static check: {summary(files, rows)}")
            return 0
        written, unchanged = write_files(args.output.resolve(), files)
        print(
            f"[PASS] generated {summary(files, rows)} "
            f"({written} written, {unchanged} unchanged)"
        )
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
