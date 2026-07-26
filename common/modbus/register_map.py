"""暫存器映射定義。

同時提供「文件地址」(4xxxx / 3xxxx / 1xxxx / 0xxxx) 與「PDU offset」(0-based)，
因為 Modbus PDU 內的起始位址與人類文件地址並不相同：
    文件地址 40001 -> PDU offset 0
    文件地址 40010 -> PDU offset 9
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

REGISTER_MAP_VERSION = 3
FIRMWARE_VERSION = 300  # 3.00


class Table(IntEnum):
    COIL = 0
    DISCRETE = 1
    INPUT = 3
    HOLDING = 4


DOC_BASE = {Table.COIL: 1, Table.DISCRETE: 10001, Table.INPUT: 30001, Table.HOLDING: 40001}


@dataclass(frozen=True)
class RegSpec:
    offset: int
    name: str
    unit: str = ""
    scale: float = 1.0
    dtype: str = "u16"  # u16 / i16 / u32 / bitfield / enum / bool
    lo: float | None = None  # 工程單位下限（寫入驗證用）
    hi: float | None = None
    writable: bool = False
    desc: str = ""
    pulse: bool = False  # 脈衝命令（處理後自動清回 OFF）

    def doc_address(self, table: Table) -> int:
        return DOC_BASE[table] + self.offset


# ---------------------------------------------------------------------------
# 共通 Coils（§11.1）
# ---------------------------------------------------------------------------
COMMON_COILS: list[RegSpec] = [
    RegSpec(0, "START", writable=True, pulse=True, desc="脈衝啟動命令"),
    RegSpec(1, "STOP", writable=True, pulse=True, desc="正常停止"),
    RegSpec(2, "RESET_TRIP", writable=True, pulse=True, desc="跳機重置脈衝，需 Reset Key"),
    RegSpec(3, "ACK_ALARM", writable=True, pulse=True, desc="警報確認"),
    RegSpec(4, "EMERGENCY_STOP", writable=True, desc="緊急停止（保持型）"),
    RegSpec(5, "FORCE_SAFE", writable=True, desc="強制安全狀態"),
    RegSpec(6, "TRIP_TEST", writable=True, pulse=True, desc="實驗模式跳機測試（需 LAB_MODE）"),
    RegSpec(7, "CLEAR_TOTALIZER", writable=True, pulse=True, desc="清除累積值"),
]

# ---------------------------------------------------------------------------
# 共通 Discrete Inputs（§11.2）
# ---------------------------------------------------------------------------
COMMON_DISCRETES: list[RegSpec] = [
    RegSpec(0, "READY"),
    RegSpec(1, "RUNNING"),
    RegSpec(2, "STARTING"),
    RegSpec(3, "STOPPING"),
    RegSpec(4, "TRIPPED"),
    RegSpec(5, "ALARM_ACTIVE"),
    RegSpec(6, "LOCAL_MODE"),
    RegSpec(7, "REMOTE_MODE"),
    RegSpec(8, "AUTO_MODE"),
    RegSpec(9, "MANUAL_MODE"),
    RegSpec(10, "CONTROL_WATCHDOG_OK"),
    RegSpec(11, "SIM_BUS_OK"),
    RegSpec(12, "INTERLOCKS_OK"),
    RegSpec(13, "SENSOR_FAULT"),
    RegSpec(14, "ACTUATOR_FAULT"),
    RegSpec(15, "MAINTENANCE_MODE"),
]

# ---------------------------------------------------------------------------
# 共通 Input Registers（§11.3）
# ---------------------------------------------------------------------------
COMMON_INPUTS: list[RegSpec] = [
    RegSpec(0, "STATUS_WORD", dtype="bitfield", desc="狀態字"),
    RegSpec(1, "ALARM_WORD_1", dtype="bitfield"),
    RegSpec(2, "ALARM_WORD_2", dtype="bitfield"),
    RegSpec(3, "TRIP_WORD", dtype="bitfield"),
    RegSpec(4, "DEVICE_STATE", dtype="enum"),
    RegSpec(5, "FIRST_OUT_CODE"),
    RegSpec(6, "OVERALL_QUALITY", dtype="enum"),
    RegSpec(7, "REGISTER_MAP_VERSION"),
    RegSpec(8, "FIRMWARE_VERSION"),
]

# 診斷區 30030～30039（offset 29～38）
COMMON_DIAGNOSTICS: list[RegSpec] = [
    RegSpec(29, "WATCHDOG_ECHO", desc="回應 40003 之值"),
    RegSpec(30, "SCAN_TIME_MS", scale=10, unit="ms", desc="物理掃描耗時 ×10"),
    RegSpec(31, "MODBUS_REQUEST_COUNT", desc="已處理請求數（模 65536）"),
    RegSpec(32, "REJECTED_COMMAND_COUNT"),
    RegSpec(33, "EXCEPTION_COUNT"),
    RegSpec(34, "SIM_QUALITY_WORD", dtype="bitfield", desc="每個輸入訊號品質是否 GOOD"),
    RegSpec(35, "SIM_TICK_LOW"),
    RegSpec(36, "COMM_LOSS_SECONDS", scale=10, unit="s"),
    RegSpec(37, "FAULT_INJECT_WORD", dtype="bitfield", desc="啟用中的故障注入"),
    RegSpec(38, "SNAPSHOT_GENERATION", desc="每次快照還原 +1，供測試判斷環境已重置"),
]

# 累積量 30040～30049（offset 39～48）
COMMON_TOTALIZERS: list[RegSpec] = [
    RegSpec(39, "RUN_SECONDS_HI", dtype="u32", unit="s"),
    RegSpec(40, "RUN_SECONDS_LO"),
    RegSpec(41, "START_COUNT"),
    RegSpec(42, "TRIP_COUNT"),
    RegSpec(43, "MASS_TOTAL_HI", dtype="u32", unit="kg"),
    RegSpec(44, "MASS_TOTAL_LO"),
    RegSpec(45, "ENERGY_TOTAL_HI", dtype="u32", unit="kWh"),
    RegSpec(46, "ENERGY_TOTAL_LO"),
    RegSpec(47, "ALARM_COUNT"),
    RegSpec(48, "ACCEPTED_COMMAND_COUNT"),
]

# ---------------------------------------------------------------------------
# 共通 Holding Registers（§11.4）
# ---------------------------------------------------------------------------
COMMON_HOLDINGS: list[RegSpec] = [
    RegSpec(0, "CONTROL_MODE", dtype="enum", lo=0, hi=4, writable=True,
            desc="0=LOCAL_MANUAL 1=LOCAL_AUTO 2=REMOTE_MANUAL 3=REMOTE_AUTO 4=MAINTENANCE"),
    RegSpec(1, "COMMAND_SEQUENCE", lo=0, hi=65535, writable=True, desc="命令序號，重置需為新值"),
    RegSpec(2, "WATCHDOG_COUNTER", lo=0, hi=65535, writable=True, desc="控制器每秒 +1"),
    RegSpec(3, "RESET_KEY", lo=0, hi=65535, writable=True, desc="重置鑰匙 0xA55A"),
    RegSpec(4, "SIMULATION_FLAGS", dtype="bitfield", lo=0, hi=65535, writable=True),
    RegSpec(5, "ACTIVE_CONTROLLER_ID", lo=0, hi=65535, writable=True),
    RegSpec(6, "COMMAND_LEASE_TIME", unit="s", lo=0, hi=3600, writable=True),
    RegSpec(7, "OUTPUT_RATE_LIMIT", scale=100, unit="%/s", lo=0, hi=655, writable=True),
    RegSpec(8, "RESERVED", lo=0, hi=65535, writable=True),
    RegSpec(9, "PRIMARY_SETPOINT", writable=True),
    RegSpec(10, "SECONDARY_SETPOINT", writable=True),
    RegSpec(11, "MANUAL_OUTPUT", scale=100, unit="%", lo=0, hi=100, writable=True),
    RegSpec(12, "OUTPUT_HIGH_LIMIT", scale=100, unit="%", lo=0, hi=100, writable=True),
    RegSpec(13, "OUTPUT_LOW_LIMIT", scale=100, unit="%", lo=0, hi=100, writable=True),
    RegSpec(19, "PID_KP", scale=100, lo=0, hi=655, writable=True),
    RegSpec(20, "PID_KI", scale=100, lo=0, hi=655, writable=True),
    RegSpec(21, "PID_KD", scale=100, lo=0, hi=655, writable=True),
    RegSpec(22, "DEADBAND", scale=100, lo=0, hi=655, writable=True),
    RegSpec(23, "INTEGRAL_LIMIT", scale=100, lo=0, hi=655, writable=True),
    RegSpec(24, "CONTROLLER_SCAN_TIME", unit="ms", lo=0, hi=10000, writable=True),
]

RESET_KEY_VALUE = 0xA55A


class ControlMode(IntEnum):
    LOCAL_MANUAL = 0
    LOCAL_AUTO = 1
    REMOTE_MANUAL = 2
    REMOTE_AUTO = 3
    MAINTENANCE = 4


class Quality(IntEnum):
    GOOD = 0
    UNCERTAIN = 1
    STALE = 2
    BAD_SENSOR = 3
    BAD_COMM = 4
    FORCED = 5
    OUT_OF_RANGE = 6
    SIMULATED_FAULT = 7


class DeviceState(IntEnum):
    OFF = 0
    STARTING = 1
    RUNNING = 2
    STOPPING = 3
    TRIPPED = 4
    PURGING = 5
    IGNITING = 6
    PRESSURIZING = 7
    MAINTENANCE = 8
    SAFE_HOLD = 9


class StatusBit(IntEnum):
    READY = 0
    RUNNING = 1
    STARTING = 2
    STOPPING = 3
    TRIPPED = 4
    ALARM_ACTIVE = 5
    REMOTE = 6
    AUTO = 7
    WATCHDOG_OK = 8
    SIM_BUS_OK = 9
    INTERLOCKS_OK = 10
    SENSOR_FAULT = 11
    ACTUATOR_FAULT = 12
    MAINTENANCE = 13
    LAB_MODE = 14
    SIM_PAUSED = 15


# ---------------------------------------------------------------------------
# RegisterMap
# ---------------------------------------------------------------------------


@dataclass
class RegisterMap:
    """單一設備的完整暫存器映射。"""

    device: str
    coils: dict[int, RegSpec] = field(default_factory=dict)
    discretes: dict[int, RegSpec] = field(default_factory=dict)
    inputs: dict[int, RegSpec] = field(default_factory=dict)
    holdings: dict[int, RegSpec] = field(default_factory=dict)
    coil_size: int = 32
    discrete_size: int = 32
    # 部分 PLC/SCADA I/O scanner 會把單一 tag 合併成從 offset 0 開始的
    # 100-word FC4 區塊讀取。保留 0～127 的連續 input image，未映射位置維持 0，
    # 避免合法 tag（例如 offset 19）因整批範圍超過舊的 64 words 而收到 Exception 02。
    input_size: int = 128
    holding_size: int = 64

    @classmethod
    def build(
        cls,
        device: str,
        process_inputs: list[RegSpec] | None = None,
        extra_holdings: list[RegSpec] | None = None,
        extra_coils: list[RegSpec] | None = None,
    ) -> "RegisterMap":
        rmap = cls(device=device)
        for spec in COMMON_COILS + (extra_coils or []):
            rmap.coils[spec.offset] = spec
        for spec in COMMON_DISCRETES:
            rmap.discretes[spec.offset] = spec
        for spec in COMMON_INPUTS + COMMON_DIAGNOSTICS + COMMON_TOTALIZERS + (process_inputs or []):
            rmap.inputs[spec.offset] = spec
        for spec in COMMON_HOLDINGS + (extra_holdings or []):
            rmap.holdings[spec.offset] = spec
        return rmap

    # -- 查詢 ---------------------------------------------------------------
    def table(self, table: Table) -> dict[int, RegSpec]:
        return {
            Table.COIL: self.coils,
            Table.DISCRETE: self.discretes,
            Table.INPUT: self.inputs,
            Table.HOLDING: self.holdings,
        }[table]

    def size(self, table: Table) -> int:
        return {
            Table.COIL: self.coil_size,
            Table.DISCRETE: self.discrete_size,
            Table.INPUT: self.input_size,
            Table.HOLDING: self.holding_size,
        }[table]

    def by_name(self, table: Table, name: str) -> RegSpec:
        for spec in self.table(table).values():
            if spec.name == name:
                return spec
        raise KeyError(f"{self.device}: {table.name} 沒有名稱為 {name} 的暫存器")

    def offset_of(self, table: Table, name: str) -> int:
        return self.by_name(table, name).offset

    def to_rows(self) -> list[dict]:
        rows: list[dict] = []
        for table in (Table.COIL, Table.DISCRETE, Table.INPUT, Table.HOLDING):
            for offset in sorted(self.table(table)):
                spec = self.table(table)[offset]
                rows.append(
                    {
                        "device": self.device,
                        "table": table.name,
                        "doc_address": spec.doc_address(table),
                        "pdu_offset": offset,
                        "name": spec.name,
                        "dtype": spec.dtype,
                        "unit": spec.unit,
                        "scale": spec.scale,
                        "min": "" if spec.lo is None else spec.lo,
                        "max": "" if spec.hi is None else spec.hi,
                        "writable": "yes" if spec.writable else "no",
                        "pulse": "yes" if spec.pulse else "no",
                        "description": spec.desc,
                    }
                )
        return rows
