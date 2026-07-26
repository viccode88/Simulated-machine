"""設備共通框架。

    Modbus Server ──► Command Queue ──► Scan Cycle ──► 狀態機 ──► 物理模型
                                                   └──► 保護/警報 ──► 原子暫存器映像

Modbus request handler 不直接修改物理變數：所有寫入先進 command queue，
下一個 scan cycle 才由狀態機與安全邏輯決定是否套用。
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any

from ..modbus.encoding import bits_to_word, enc_u16, enc_u32
from ..modbus.exceptions import ModbusException
from ..modbus.register_map import (
    FIRMWARE_VERSION,
    REGISTER_MAP_VERSION,
    RESET_KEY_VALUE,
    ControlMode,
    DeviceState,
    Quality,
    RegisterMap,
    RegSpec,
    StatusBit,
    Table,
)
from ..modbus.server import (
    AccessPolicy,
    CommFaults,
    DeviceIdentification,
    ModbusTcpServer,
    RegisterImage,
    WriteRequest,
)
from ..simbus.client import SimBusClient
from ..simbus.protocol import DEFAULT_BUS_PORT, MsgType, Role, SignalValue
from ..util import EventLogger, cfg_get, clamp, env_bool, install_excepthook, load_config, wall_time_iso
from .alarm import AlarmManager, AlarmSpec
from .faults import FaultInjector
from .persistence import StateStore
from .protection import ProtectionEngine, build_protections
from .state_machine import DEFAULT_TRANSITIONS, StateMachine

COMM_POLICIES = ("HOLD_LAST", "FAIL_LOW", "FAIL_HIGH", "FAIL_CLOSE", "FAIL_OPEN", "LOCAL_FALLBACK", "TRIP")

SNAPSHOT_VERSION = 1


def common_protection_defs(base: int) -> list[dict]:
    """所有設備共用、只能由程式觸發（非門檻式）的跳機。"""
    return [
        {"code": base + 97, "name": "COMM_TIMEOUT_TRIP", "signal": "__manual__",
         "message": "控制通訊逾時跳機", "trips": False},
        {"code": base + 98, "name": "TRIP_TEST", "signal": "__manual__",
         "message": "實驗模式跳機測試", "trips": False},
        {"code": base + 99, "name": "EMERGENCY_STOP", "signal": "__manual__",
         "message": "緊急停止", "trips": False},
    ]


def common_alarms(base: int) -> list[AlarmSpec]:
    """所有設備共用的警報，佔 Alarm Word 2 的高位元。"""
    return [
        AlarmSpec(base + 90, "COMMAND_REJECTED", 26, "命令被拒絕"),
        AlarmSpec(base + 91, "CONTROL_WATCHDOG_LOST", 27, "控制器 watchdog 逾時"),
        AlarmSpec(base + 92, "SIM_BUS_BAD", 28, "模擬匯流排資料品質不良"),
        AlarmSpec(base + 93, "SENSOR_FAULT", 29, "感測器故障"),
        AlarmSpec(base + 94, "ACTUATOR_FAULT", 30, "執行器故障"),
        AlarmSpec(base + 95, "FAULT_INJECTED", 31, "實驗模式故障注入啟用中"),
    ]


class BaseDevice:
    # -- 子類別必須/可以覆寫 ------------------------------------------------
    NAME: str = "device"
    CODE_BASE: int = 5000
    PROCESS_INPUTS: list[RegSpec] = []
    EXTRA_HOLDINGS: list[RegSpec] = []
    EXTRA_COILS: list[RegSpec] = []
    ALARMS: list[AlarmSpec] = []
    PROTECTION_DEFS: list[dict] = []
    PUBLISHES: list[str] = []
    SUBSCRIBES: list[str] = []
    STATE_VARS: list[str] = []
    TRANSITIONS = DEFAULT_TRANSITIONS
    DEFAULT_COMM_POLICY = "HOLD_LAST"
    HAS_START_STOP = True

    # -- 初始化 ------------------------------------------------------------
    def __init__(self, config_dir: str = "/app/configs", state_dir: str = "/var/lib/plant-device") -> None:
        self.cfg = load_config(
            os.path.join(config_dir, "plant.yaml"),
            os.path.join(config_dir, f"{self.NAME}.yaml"),
        )
        self.lab_mode = env_bool("LAB_MODE", bool(cfg_get(self.cfg, "lab_mode", False)))
        self.state_dir = os.environ.get("STATE_DIR", state_dir)
        self.log = EventLogger(
            device=self.NAME,
            path=os.path.join(self.state_dir, "events.jsonl"),
            sim_time_fn=lambda: self.sim_time,
        )
        install_excepthook(self.log)

        # 時間
        self.sim_time = 0.0
        self.tick = 0
        self.dt = float(cfg_get(self.cfg, "simulation.dt", 0.1))
        self.scan_time_ms = 0.0

        # 狀態機
        self.sm = StateMachine(DeviceState.OFF, self.TRANSITIONS, on_change=self._on_state_change,
                               on_reject=self._on_state_reject)

        # 警報 / 保護
        self.alarms = AlarmManager(list(self.ALARMS) + common_alarms(self.CODE_BASE), emit=self._emit)
        self.protection = ProtectionEngine(
            self.NAME,
            build_protections(
                self.NAME, self.cfg, list(self.PROTECTION_DEFS) + common_protection_defs(self.CODE_BASE)
            ),
            emit=self._emit,
            alarm_hook=self._protection_alarm,
            wall_time_fn=wall_time_iso,
        )
        self._last_reject_sim_time = -1e9

        # 故障注入
        self.faults = FaultInjector(enabled=self.lab_mode)
        self.comm_faults = CommFaults()
        # 感測器故障每個 scan cycle 只取樣一次（見 sensor_sample）
        self._sensor_epoch = 0
        self._sensor_cache: dict[str, tuple[int, float, int]] = {}

        # 暫存器
        self.rmap = RegisterMap.build(self.NAME, self.PROCESS_INPUTS, self.EXTRA_HOLDINGS, self.EXTRA_COILS)
        self.hold: list[int] = [0] * self.rmap.holding_size
        self.coil: list[bool] = [False] * self.rmap.coil_size
        self._image = RegisterImage.empty(self.rmap)
        self._cmd_queue: deque[WriteRequest] = deque(maxlen=512)
        self._busy = False
        self._background: list[asyncio.Task] = []
        self._running = True

        # 通訊監控
        self.watchdog_value = 0
        # watchdog 以「模擬時間」計時，確保不同模擬速度下行為一致、且快照可重現
        self.watchdog_age = 0.0
        self.watchdog_ok = False
        self.comm_loss_seconds = 0.0
        self.comm_policy = str(cfg_get(self.cfg, "comm.failure_policy", self.DEFAULT_COMM_POLICY)).upper()
        self.comm_hold_seconds = float(cfg_get(self.cfg, "comm.hold_seconds", 2.0))
        self.watchdog_timeout = float(cfg_get(self.cfg, "comm.watchdog_timeout", 3.0))

        # 模擬匯流排
        self.inputs: dict[str, SignalValue] = {}
        self._last_good: dict[str, float] = {}
        self.bus_ok = False
        self.bus_paused = False
        self._last_tick_wall = time.monotonic()

        # 累積量
        self.run_seconds = 0.0
        self.start_count = 0
        self.trip_count = 0
        self.mass_total = 0.0
        self.energy_total = 0.0
        self.accepted_commands = 0
        self.rejected_commands = 0
        self.snapshot_generation = 0
        self.boot_count = 0
        self._last_command_sequence = -1

        # 持久化
        self.store = StateStore(os.path.join(self.state_dir, f"{self.NAME}.db"), self.NAME)

        # Modbus server
        access = AccessPolicy(
            write_allowlist=list(cfg_get(self.cfg, "modbus.write_allowlist", []) or []),
            safety_allowlist=list(cfg_get(self.cfg, "modbus.safety_allowlist", []) or []),
            lease_seconds=float(cfg_get(self.cfg, "modbus.lease_seconds", 5.0)),
            enforce_single_writer=bool(cfg_get(self.cfg, "modbus.single_writer", True)),
        )
        self.server = ModbusTcpServer(
            self.rmap,
            image_provider=lambda: self._image,
            write_handler=self._on_write,
            unit_id=int(cfg_get(self.cfg, "modbus.unit_id", 1)),
            host=os.environ.get("MODBUS_HOST", "0.0.0.0"),
            port=int(os.environ.get("MODBUS_PORT", cfg_get(self.cfg, "modbus.port", 502))),
            max_clients=int(cfg_get(self.cfg, "modbus.max_clients", 32)),
            busy_provider=lambda: self._busy,
            on_request=self._on_modbus_request,
            identification=DeviceIdentification(model_name=self.NAME.upper(), app_name=self.NAME),
            access=access,
            faults=self.comm_faults,
            lab_mode=self.lab_mode,
        )
        self.log_modbus = env_bool("MODBUS_TRACE", bool(cfg_get(self.cfg, "modbus.trace", False)))

        # 模擬匯流排 client
        self.bus = SimBusClient(
            self.NAME,
            os.environ.get("SIM_BUS_HOST", str(cfg_get(self.cfg, "simulation.bus_host", "plant-bus"))),
            int(os.environ.get("SIM_BUS_PORT", cfg_get(self.cfg, "simulation.bus_port", DEFAULT_BUS_PORT))),
            role=Role.DEVICE,
            publishes=list(self.PUBLISHES),
            subscribes=list(self.SUBSCRIBES),
        )

        self.configure()
        self._init_holdings()
        self._restore_persisted()
        self._rebuild_image()

    # -- 子類別掛勾（預設為空） ---------------------------------------------
    def configure(self) -> None: ...
    def default_holdings(self) -> dict[str, float]:
        return {}

    def start_permissives(self) -> list[tuple[str, bool]]:
        return []

    def on_start(self) -> None: ...
    def on_stop(self) -> None: ...
    def on_trip(self, codes: list[int]) -> None: ...
    def step(self, dt: float) -> None: ...
    def protection_values(self) -> dict[str, float]:
        return {}

    def publish(self) -> dict[str, float]:
        return {}

    def fill_registers(self, regs: list[int]) -> None: ...
    def apply_comm_loss(self, policy: str) -> None: ...
    def control_output(self) -> float:
        return 0.0

    def snapshot_extra(self) -> dict:
        return {}

    def restore_extra(self, data: dict) -> None: ...

    def on_reset(self) -> None: ...

    # -- 事件 --------------------------------------------------------------
    def _emit(self, event: str, **fields: Any) -> None:
        record = self.log.emit(event, **fields)
        if self.bus.connected.is_set():
            asyncio.ensure_future(self.bus.send_event(record))

    def _on_state_change(self, old: DeviceState, new: DeviceState, reason: str) -> None:
        self._emit("STATE_CHANGE", old=old.name, new=new.name, reason=reason)
        if new is DeviceState.RUNNING and old is not DeviceState.RUNNING:
            self.start_count += 1

    def _on_state_reject(self, current: DeviceState, target: DeviceState, reason: str) -> None:
        self._emit("STATE_TRANSITION_REJECTED", current=current.name, target=target.name,
                   reason=reason)

    def _protection_alarm(self, code: int, active: bool, value: float, threshold: float) -> None:
        self.alarms.set(code, active, value, threshold)

    # -- Modbus 寫入 -> command queue ---------------------------------------
    def _on_write(self, request: WriteRequest) -> ModbusException | None:
        if len(self._cmd_queue) >= self._cmd_queue.maxlen:  # type: ignore[operator]
            return ModbusException.SERVER_DEVICE_BUSY
        self._cmd_queue.append(request)
        return None

    def _on_modbus_request(self, record: dict) -> None:
        if self.log_modbus or record.get("exception_code") or record.get("error"):
            self.log.emit("MODBUS_REQUEST", **record)

    # -- Holding 初始化 -----------------------------------------------------
    def _init_holdings(self) -> None:
        self.hold[self.rmap.offset_of(Table.HOLDING, "CONTROL_MODE")] = int(
            cfg_get(self.cfg, "control.default_mode", ControlMode.REMOTE_AUTO)
        )
        self.hold[self.rmap.offset_of(Table.HOLDING, "OUTPUT_HIGH_LIMIT")] = 10000
        self.hold[self.rmap.offset_of(Table.HOLDING, "OUTPUT_LOW_LIMIT")] = 0
        self.hold[self.rmap.offset_of(Table.HOLDING, "COMMAND_LEASE_TIME")] = int(
            cfg_get(self.cfg, "modbus.lease_seconds", 5)
        )
        for name, value in self.default_holdings().items():
            spec = self.rmap.by_name(Table.HOLDING, name)
            self.hold[spec.offset] = enc_u16(value, spec.scale)

    def hr(self, name: str) -> float:
        """以工程單位讀取 Holding Register。"""
        spec = self.rmap.by_name(Table.HOLDING, name)
        raw = self.hold[spec.offset]
        if spec.dtype == "i16" and raw >= 0x8000:
            raw -= 0x10000
        return raw / spec.scale

    def set_hr(self, name: str, value: float) -> None:
        spec = self.rmap.by_name(Table.HOLDING, name)
        self.hold[spec.offset] = enc_u16(value, spec.scale)

    @property
    def control_mode(self) -> ControlMode:
        try:
            return ControlMode(self.hold[self.rmap.offset_of(Table.HOLDING, "CONTROL_MODE")])
        except ValueError:
            return ControlMode.LOCAL_MANUAL

    @property
    def auto_mode(self) -> bool:
        return self.control_mode in (ControlMode.LOCAL_AUTO, ControlMode.REMOTE_AUTO)

    @property
    def remote_mode(self) -> bool:
        return self.control_mode in (ControlMode.REMOTE_MANUAL, ControlMode.REMOTE_AUTO)

    @property
    def maintenance(self) -> bool:
        return self.control_mode is ControlMode.MAINTENANCE

    @property
    def estop(self) -> bool:
        return self.coil[self.rmap.offset_of(Table.COIL, "EMERGENCY_STOP")]

    @property
    def force_safe(self) -> bool:
        return self.coil[self.rmap.offset_of(Table.COIL, "FORCE_SAFE")]

    # -- 訊號存取 ----------------------------------------------------------
    def sig(self, name: str, default: float = 0.0) -> float:
        signal = self.inputs.get(name)
        if signal is None:
            return self._last_good.get(name, default)
        if signal.usable:
            self._last_good[name] = signal.value
            return signal.value
        return self._last_good.get(name, default)

    def sig_quality(self, name: str) -> str:
        signal = self.inputs.get(name)
        return signal.quality if signal else "BAD"

    def sig_good(self, name: str) -> bool:
        signal = self.inputs.get(name)
        return bool(signal and signal.good)

    # -- 感測器取樣 --------------------------------------------------------
    def sensor_sample(self, name: str, true_value: float) -> tuple[float, int]:
        """套用感測器故障，且每個 scan cycle 只取樣一次。

        保護邏輯、Modbus 暫存器映像與 SimBus 發佈值必須看到同一個讀值；
        若各自呼叫 faults.sensor()，drift 會依呼叫次數倍增，noise/intermittent
        則會讓三處數值互相矛盾。
        """
        cached = self._sensor_cache.get(name)
        if cached is not None and cached[0] == self._sensor_epoch:
            return cached[1], cached[2]
        value, quality = self.faults.sensor(name, true_value, self.dt)
        self._sensor_cache[name] = (self._sensor_epoch, value, quality)
        return value, quality

    # -- 命令處理 ----------------------------------------------------------
    def _reject(self, reason: str, **fields: Any) -> None:
        self.rejected_commands += 1
        self._last_reject_sim_time = self.sim_time
        self.alarms.set(self.CODE_BASE + 90, True)
        self._emit("COMMAND_REJECTED", code=self.CODE_BASE + 90, reason=reason, **fields)

    def set_inhibit(self, protection_name: str, predicate) -> None:
        """讓設備在 configure() 內對特定保護加上抑制條件（例如停機時不評估）。"""
        for spec in self.protection.specs.values():
            if spec.name == protection_name:
                spec.inhibit = predicate

    def _apply_commands(self) -> None:
        """把 command queue 內容套進 self.hold / self.coil，再由狀態機決定行為。"""
        while self._cmd_queue:
            request = self._cmd_queue.popleft()
            if request.table is Table.HOLDING:
                for index, value in enumerate(request.values):
                    self.hold[request.offset + index] = value & 0xFFFF
                self.accepted_commands += 1
            elif request.table is Table.COIL:
                for index, value in enumerate(request.values):
                    self.coil[request.offset + index] = bool(value)
                self.accepted_commands += 1

        offset = self.rmap.offset_of
        # --- 脈衝命令 ---
        if self.coil[offset(Table.COIL, "START")]:
            self.coil[offset(Table.COIL, "START")] = False
            self._handle_start()
        if self.coil[offset(Table.COIL, "STOP")]:
            self.coil[offset(Table.COIL, "STOP")] = False
            self._handle_stop()
        if self.coil[offset(Table.COIL, "RESET_TRIP")]:
            self.coil[offset(Table.COIL, "RESET_TRIP")] = False
            self._handle_reset()
        if self.coil[offset(Table.COIL, "ACK_ALARM")]:
            self.coil[offset(Table.COIL, "ACK_ALARM")] = False
            count = self.alarms.ack_all()
            self._emit("ALARM_ACK_ALL", count=count)
        if self.coil[offset(Table.COIL, "TRIP_TEST")]:
            self.coil[offset(Table.COIL, "TRIP_TEST")] = False
            self._handle_trip_test()
        if self.coil[offset(Table.COIL, "CLEAR_TOTALIZER")]:
            self.coil[offset(Table.COIL, "CLEAR_TOTALIZER")] = False
            self.mass_total = 0.0
            self.energy_total = 0.0
            self._emit("TOTALIZER_CLEARED")

        # --- 緊急停止 ---
        if self.estop and not self.sm.tripped:
            self.protection.force_trip(self.CODE_BASE + 99, self.sim_time,
                                       control_output=self.control_output(),
                                       message="緊急停止")
            self._trip("EMERGENCY_STOP")

        # --- watchdog ---
        watchdog = self.hold[offset(Table.HOLDING, "WATCHDOG_COUNTER")]
        if watchdog != self.watchdog_value:
            self.watchdog_value = watchdog
            self.watchdog_age = 0.0

    def _handle_start(self) -> None:
        if self.sm.tripped or self.protection.any_latched:
            self._reject("跳機未重置", command="START")
            return
        if self.estop:
            self._reject("緊急停止啟動中", command="START")
            return
        if self.maintenance:
            self._reject("維修模式", command="START")
            return
        blocked = [name for name, ok in self.start_permissives() if not ok]
        if blocked:
            self._reject("啟動允許條件不成立", command="START", blocked=blocked)
            return
        if self.sm.running or self.sm.starting:
            return
        self.on_start()
        self._emit("START_ACCEPTED")

    def _handle_stop(self) -> None:
        if self.sm.in_any([DeviceState.OFF, DeviceState.TRIPPED]):
            return
        self.on_stop()
        self._emit("STOP_ACCEPTED")

    def _handle_reset(self) -> None:
        offset = self.rmap.offset_of
        key = self.hold[offset(Table.HOLDING, "RESET_KEY")]
        sequence = self.hold[offset(Table.HOLDING, "COMMAND_SEQUENCE")]
        if key != RESET_KEY_VALUE:
            self._reject("Reset Key 錯誤", command="RESET_TRIP", key=key)
            return
        if self.estop:
            self._reject("緊急停止尚未解除", command="RESET_TRIP")
            return
        if sequence == self._last_command_sequence:
            self._reject("命令序號未更新", command="RESET_TRIP", sequence=sequence)
            return
        allowed, why = self.protection.can_reset()
        if not allowed:
            self._reject(why or "安全條件不成立", command="RESET_TRIP")
            return
        self._last_command_sequence = sequence
        self.protection.reset()
        self.alarms.ack_all()
        if self.sm.tripped:
            self.sm.force(DeviceState.OFF, "RESET")
        self.on_reset()
        self._emit("TRIP_RESET", sequence=sequence)

    def _handle_trip_test(self) -> None:
        if not self.lab_mode:
            self._reject("TRIP_TEST 僅在 LAB_MODE 開放", command="TRIP_TEST")
            return
        self.protection.force_trip(self.CODE_BASE + 98, self.sim_time,
                                   control_output=self.control_output(), message="跳機測試")
        self._trip("TRIP_TEST")

    def _trip(self, reason: str, codes: list[int] | None = None) -> None:
        first = not self.sm.tripped
        self.sm.force(DeviceState.TRIPPED, reason)
        if first:
            self.trip_count += 1
        self.on_trip(codes or [])

    # -- 掃描週期 ----------------------------------------------------------
    def scan(self, dt: float) -> None:
        started = time.perf_counter()
        self._busy = True
        # 新的 scan cycle：感測器故障重新取樣一次，之後同一 tick 內共用同一讀值
        self._sensor_epoch += 1
        try:
            self._apply_commands()
            self._update_comm_status(dt)
            self.sm.tick(dt)
            if not self.sm.tripped:
                self.step(dt)
            else:
                self.step(dt)  # 跳機後仍需計算慣性/散熱等物理
            values = self.protection_values()
            new_trips = self.protection.evaluate(dt, values, self.sim_time, self.control_output())
            if new_trips:
                self._trip("PROTECTION", new_trips)
            self._update_manual_protections()
            self._update_common_alarms()
            self._update_totalizers(dt)
        finally:
            self._busy = False
        self._rebuild_image()
        self.scan_time_ms = (time.perf_counter() - started) * 1000.0

    def _update_comm_status(self, dt: float) -> None:
        self.watchdog_age += dt
        age = self.watchdog_age
        was_ok = self.watchdog_ok
        self.watchdog_ok = age < self.watchdog_timeout and self.watchdog_value != 0
        if self.watchdog_ok:
            self.comm_loss_seconds = 0.0
        else:
            self.comm_loss_seconds += dt
            if was_ok:
                self._emit("CONTROL_WATCHDOG_LOST", age=round(age, 2), policy=self.comm_policy)
            if self.comm_loss_seconds >= self.comm_hold_seconds:
                self.apply_comm_loss(self.comm_policy)
                if self.comm_policy == "TRIP" and not self.sm.tripped:
                    self.protection.force_trip(self.CODE_BASE + 97, self.sim_time,
                                               control_output=self.control_output(),
                                               message="控制通訊逾時")
                    self._trip("COMM_TIMEOUT")
        self.alarms.set(self.CODE_BASE + 91, not self.watchdog_ok and self.watchdog_value != 0)

    def _update_manual_protections(self) -> None:
        """程式觸發型跳機的 active/resettable 由對應條件維持。"""
        conditions = {
            self.CODE_BASE + 99: self.estop,
            self.CODE_BASE + 98: False,
            self.CODE_BASE + 97: (not self.watchdog_ok) and self.comm_policy == "TRIP",
        }
        for code, condition in conditions.items():
            state = self.protection.states.get(code)
            if state is None or not state.latched:
                continue
            if state.active and not condition:
                state.active = False
                self._emit("TRIP_CONDITION_CLEARED", code=code,
                           name=self.protection.specs[code].name, value=0.0)
            state.resettable = not state.active

    def _update_common_alarms(self) -> None:
        self.alarms.set(self.CODE_BASE + 90, self.sim_time - self._last_reject_sim_time < 2.0)
        bad_inputs = [name for name in self.SUBSCRIBES if not self.sig_good(name)]
        self.alarms.set(self.CODE_BASE + 92, bool(bad_inputs) or not self.bus_ok)
        self.alarms.set(self.CODE_BASE + 93, any(f.mode != "none" for f in self.faults.sensors.values()))
        self.alarms.set(self.CODE_BASE + 94, bool(self.faults.actuators))
        self.alarms.set(self.CODE_BASE + 95, self.faults.enabled and bool(
            self.faults.sensors or self.faults.actuators or self.faults.process
        ))

    def _update_totalizers(self, dt: float) -> None:
        if self.sm.running:
            self.run_seconds += dt

    # -- 暫存器映像 --------------------------------------------------------
    def _status_word(self) -> int:
        return bits_to_word(
            {
                StatusBit.READY: self._ready(),
                StatusBit.RUNNING: self.sm.running,
                StatusBit.STARTING: self.sm.starting,
                StatusBit.STOPPING: self.sm.stopping,
                StatusBit.TRIPPED: self.sm.tripped or self.protection.any_latched,
                StatusBit.ALARM_ACTIVE: self.alarms.any_active,
                StatusBit.REMOTE: self.remote_mode,
                StatusBit.AUTO: self.auto_mode,
                StatusBit.WATCHDOG_OK: self.watchdog_ok,
                StatusBit.SIM_BUS_OK: self.bus_ok,
                StatusBit.INTERLOCKS_OK: self._interlocks_ok(),
                StatusBit.SENSOR_FAULT: bool(self.faults.sensors),
                StatusBit.ACTUATOR_FAULT: bool(self.faults.actuators),
                StatusBit.MAINTENANCE: self.maintenance,
                StatusBit.LAB_MODE: self.lab_mode,
                StatusBit.SIM_PAUSED: self.bus_paused,
            }
        )

    def _ready(self) -> bool:
        return (
            not self.sm.tripped
            and not self.protection.any_latched
            and not self.estop
            and all(ok for _, ok in self.start_permissives())
        )

    def _interlocks_ok(self) -> bool:
        return all(ok for _, ok in self.start_permissives())

    def overall_quality(self) -> Quality:
        if not self.bus_ok:
            return Quality.BAD_COMM
        if self.faults.enabled and self.faults.sensors:
            return Quality.SIMULATED_FAULT
        qualities = [self.sig_quality(name) for name in self.SUBSCRIBES]
        if any(q == "BAD" for q in qualities):
            return Quality.BAD_COMM
        if any(q == "STALE" for q in qualities):
            return Quality.STALE
        return Quality.GOOD

    def _sim_quality_word(self) -> int:
        return bits_to_word([self.sig_good(name) for name in self.SUBSCRIBES[:16]])

    def _rebuild_image(self) -> None:
        offset = self.rmap.offset_of
        regs = [0] * self.rmap.input_size
        alarm1, alarm2 = self.alarms.words()
        regs[0] = self._status_word()
        regs[1] = alarm1
        regs[2] = alarm2
        regs[3] = self.protection.trip_word()
        regs[4] = int(self.sm.state)
        regs[5] = self.protection.first_out_code() % 65536
        regs[6] = int(self.overall_quality())
        regs[7] = REGISTER_MAP_VERSION
        regs[8] = FIRMWARE_VERSION

        regs[29] = self.watchdog_value & 0xFFFF
        regs[30] = enc_u16(self.scan_time_ms, 10)
        regs[31] = self.server.request_count & 0xFFFF
        regs[32] = self.rejected_commands & 0xFFFF
        regs[33] = self.server.exception_count & 0xFFFF
        regs[34] = self._sim_quality_word()
        regs[35] = self.tick & 0xFFFF
        regs[36] = enc_u16(self.comm_loss_seconds, 10)
        regs[37] = self.faults.active_word() | (self.comm_faults.active_word() << 8)
        regs[38] = self.snapshot_generation & 0xFFFF

        high, low = enc_u32(self.run_seconds)
        regs[39], regs[40] = high, low
        regs[41] = self.start_count & 0xFFFF
        regs[42] = self.trip_count & 0xFFFF
        high, low = enc_u32(self.mass_total)
        regs[43], regs[44] = high, low
        high, low = enc_u32(self.energy_total)
        regs[45], regs[46] = high, low
        regs[47] = self.alarms.total_count & 0xFFFF
        regs[48] = self.accepted_commands & 0xFFFF

        self.fill_registers(regs)

        discretes = [False] * self.rmap.discrete_size
        discretes[0] = self._ready()
        discretes[1] = self.sm.running
        discretes[2] = self.sm.starting
        discretes[3] = self.sm.stopping
        discretes[4] = self.sm.tripped or self.protection.any_latched
        discretes[5] = self.alarms.any_active
        discretes[6] = not self.remote_mode
        discretes[7] = self.remote_mode
        discretes[8] = self.auto_mode
        discretes[9] = not self.auto_mode
        discretes[10] = self.watchdog_ok
        discretes[11] = self.bus_ok
        discretes[12] = self._interlocks_ok()
        discretes[13] = bool(self.faults.sensors)
        discretes[14] = bool(self.faults.actuators)
        discretes[15] = self.maintenance

        self._image = RegisterImage(
            coils=tuple(self.coil),
            discretes=tuple(discretes),
            inputs=tuple(int(v) & 0xFFFF for v in regs),
            holdings=tuple(int(v) & 0xFFFF for v in self.hold),
            generation=self.snapshot_generation,
        )

    # -- 快照 --------------------------------------------------------------
    def snapshot_state(self) -> dict:
        return {
            "version": SNAPSHOT_VERSION,
            "device": self.NAME,
            "sim_time": self.sim_time,
            "tick": self.tick,
            "state_machine": self.sm.to_dict(),
            "physics": {name: getattr(self, name) for name in self.STATE_VARS},
            "protection": self.protection.to_dict(),
            "alarms": self.alarms.to_dict(),
            "holdings": list(self.hold),
            "coils": list(self.coil),
            "faults": self.faults.to_dict(),
            "comm_faults": {
                "response_delay_ms": self.comm_faults.response_delay_ms,
                "drop_request_prob": self.comm_faults.drop_request_prob,
                "drop_response_prob": self.comm_faults.drop_response_prob,
                "force_busy_prob": self.comm_faults.force_busy_prob,
                "wrong_exception_prob": self.comm_faults.wrong_exception_prob,
                "connection_reset_prob": self.comm_faults.connection_reset_prob,
                "freeze": self.comm_faults.freeze,
                "rate_limit_per_s": self.comm_faults.rate_limit_per_s,
            },
            "totalizers": {
                "run_seconds": self.run_seconds,
                "start_count": self.start_count,
                "trip_count": self.trip_count,
                "mass_total": self.mass_total,
                "energy_total": self.energy_total,
                "accepted_commands": self.accepted_commands,
                "rejected_commands": self.rejected_commands,
            },
            "last_good": dict(self._last_good),
            "last_command_sequence": self._last_command_sequence,
            "comm": {"watchdog_value": self.watchdog_value, "watchdog_age": self.watchdog_age,
                     "comm_loss_seconds": self.comm_loss_seconds},
            "extra": self.snapshot_extra(),
        }

    def restore_state(self, data: dict, options: dict | None = None) -> None:
        options = options or {}
        self.sim_time = float(data.get("sim_time", self.sim_time))
        self.tick = int(data.get("tick", self.tick))
        self.sm.from_dict(data.get("state_machine") or {})
        for name, value in (data.get("physics") or {}).items():
            if name in self.STATE_VARS:
                setattr(self, name, value)
        self.protection.from_dict(data.get("protection") or {})
        self.alarms.from_dict(data.get("alarms") or {})
        holdings = data.get("holdings") or []
        for index, value in enumerate(holdings[: self.rmap.holding_size]):
            self.hold[index] = int(value) & 0xFFFF
        coils = data.get("coils") or []
        for index, value in enumerate(coils[: self.rmap.coil_size]):
            self.coil[index] = bool(value)
        if not options.get("keep_faults", False):
            self.faults.from_dict(data.get("faults") or {})
            # 故障注入開關永遠由本機 LAB_MODE 決定，不得由快照內容重新啟用：
            # 否則 LAB_MODE=false 的機組會帶著故障運轉，且無法再由 API 清除
            self.faults.enabled = self.lab_mode
            if not self.lab_mode:
                self.faults.clear()
                self.comm_faults.reset()
                if data.get("faults") or data.get("comm_faults"):
                    self._emit("SNAPSHOT_FAULTS_DISCARDED", reason="LAB_MODE 未開啟")
            else:
                comm = data.get("comm_faults") or {}
                for key, value in comm.items():
                    if hasattr(self.comm_faults, key):
                        setattr(self.comm_faults, key, value)
        totals = data.get("totalizers") or {}
        if not options.get("preserve_totalizers", False):
            self.run_seconds = float(totals.get("run_seconds", 0.0))
            self.start_count = int(totals.get("start_count", 0))
            self.trip_count = int(totals.get("trip_count", 0))
            self.mass_total = float(totals.get("mass_total", 0.0))
            self.energy_total = float(totals.get("energy_total", 0.0))
            self.accepted_commands = int(totals.get("accepted_commands", 0))
            self.rejected_commands = int(totals.get("rejected_commands", 0))
        self._last_good = dict(data.get("last_good") or {})
        self._last_command_sequence = int(data.get("last_command_sequence", -1))
        comm = data.get("comm") or {}
        self.watchdog_value = int(comm.get("watchdog_value", self.watchdog_value))
        self.comm_loss_seconds = float(comm.get("comm_loss_seconds", 0.0))
        self.restore_extra(data.get("extra") or {})

        if options.get("clear_latches", False):
            self.protection.clear_all_latches()
            self.alarms.ack_all()
            if self.sm.tripped:
                self.sm.force(DeviceState.OFF, "SNAPSHOT_CLEAN_RESTORE")

        # 還原後丟棄尚未套用的命令，避免舊命令污染新環境
        self._cmd_queue.clear()
        self.snapshot_generation = (self.snapshot_generation + 1) & 0xFFFF
        self.watchdog_age = 0.0
        self._rebuild_image()
        self.store.save(self._persist_payload())
        self._emit("SNAPSHOT_RESTORED", generation=self.snapshot_generation,
                   sim_time=round(self.sim_time, 3), options=options)

    # -- 持久化 ------------------------------------------------------------
    def _persist_payload(self) -> dict:
        return {
            "protection": self.protection.to_dict(),
            "alarms": self.alarms.to_dict(),
            "totalizers": {
                "run_seconds": self.run_seconds,
                "start_count": self.start_count,
                "trip_count": self.trip_count,
                "mass_total": self.mass_total,
                "energy_total": self.energy_total,
                "accepted_commands": self.accepted_commands,
                "rejected_commands": self.rejected_commands,
            },
            "snapshot_generation": self.snapshot_generation,
            "last_command_sequence": self._last_command_sequence,
        }

    def _restore_persisted(self) -> None:
        data = self.store.load()
        self.boot_count = self.store.log_boot(f"{self.NAME} start")
        if not data:
            self._emit("COLD_START", boot_count=self.boot_count)
            return
        self.protection.from_dict(data.get("protection") or {})
        self.alarms.from_dict(data.get("alarms") or {})
        totals = data.get("totalizers") or {}
        self.run_seconds = float(totals.get("run_seconds", 0.0))
        self.start_count = int(totals.get("start_count", 0))
        self.trip_count = int(totals.get("trip_count", 0))
        self.mass_total = float(totals.get("mass_total", 0.0))
        self.energy_total = float(totals.get("energy_total", 0.0))
        self.accepted_commands = int(totals.get("accepted_commands", 0))
        self.rejected_commands = int(totals.get("rejected_commands", 0))
        self.snapshot_generation = int(data.get("snapshot_generation", 0))
        self._last_command_sequence = int(data.get("last_command_sequence", -1))
        # 容器重啟後：跳機鎖存保留，設備回到安全輸出（OFF/TRIPPED）
        if self.protection.any_latched:
            self.sm.force(DeviceState.TRIPPED, "RESTART_WITH_LATCHED_TRIP")
        self._emit(
            "WARM_START",
            boot_count=self.boot_count,
            latched=self.protection.any_latched,
            first_out=self.protection.first_out_code(),
        )

    # -- 主迴圈 ------------------------------------------------------------
    async def run(self) -> None:
        await self.server.start()
        self._emit("MODBUS_SERVER_STARTED", port=self.server.port, unit_id=self.server.unit_id)
        self.bus.start()
        self._background.append(asyncio.ensure_future(self._persist_loop()))
        free_run_timeout = max(0.05, self.dt * 3)
        # 以旗標控制主迴圈：確保 shutdown() 一定能停下來
        # （Python 3.10 的 asyncio.wait_for 在極少數競態下會吞掉 CancelledError）
        while self._running:
            message = await self.bus.next_message(timeout=free_run_timeout)
            if message is None:
                await self._handle_no_message()
                continue
            await self._handle_message(message)

    async def _handle_no_message(self) -> None:
        connected = self.bus.connected.is_set()
        if connected and self.bus_paused:
            return  # 模擬暫停中（例如快照作業）
        age = time.monotonic() - self._last_tick_wall
        if connected and age < max(1.0, self.dt * 10):
            return
        # 匯流排失聯：使用上一筆程序量，品質降級，仍維持本地安全邏輯
        if self.bus_ok:
            self._emit("SIM_BUS_TIMEOUT", age=round(age, 2))
        self.bus_ok = False
        for name, signal in self.inputs.items():
            signal.quality = "STALE" if age < 5.0 else "BAD"
        self.sim_time += self.dt
        self.tick += 1
        self.scan(self.dt)
        self._last_tick_wall = time.monotonic()
        await asyncio.sleep(0)

    async def _handle_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == MsgType.TICK.value:
            await self._handle_tick(message)
        elif kind == MsgType.WELCOME.value:
            self.bus_ok = True
            self.bus_paused = bool(message.get("paused", False))
            self.dt = float(message.get("dt", self.dt))
            self._emit("SIM_BUS_CONNECTED", tick=message.get("tick"), dt=self.dt)
        elif kind == MsgType.SNAPSHOT_SAVE.value:
            await self.bus.send(
                {
                    "type": MsgType.SNAPSHOT_DATA.value,
                    "device": self.NAME,
                    "request_id": message.get("request_id"),
                    "state": self.snapshot_state(),
                }
            )
        elif kind == MsgType.SNAPSHOT_RESTORE.value:
            ok, error = True, ""
            try:
                state = message.get("state")
                if state:
                    self.restore_state(state, message.get("options") or {})
                else:
                    ok, error = False, "快照不含此設備狀態"
            except Exception as exc:  # pragma: no cover - 防禦性
                ok, error = False, repr(exc)
                self._emit("SNAPSHOT_RESTORE_FAILED", error=error)
            await self.bus.send(
                {
                    "type": MsgType.RESTORE_ACK.value,
                    "device": self.NAME,
                    "request_id": message.get("request_id"),
                    "ok": ok,
                    "error": error,
                    "generation": self.snapshot_generation,
                }
            )
        elif kind == MsgType.FAULT.value:
            try:
                self._handle_fault(message)
            except Exception as exc:  # 故障注入永遠不得使設備主迴圈退出
                self._emit("FAULT_HANDLER_ERROR", error=repr(exc),
                           payload=message.get("payload"))
            await self.bus.send(
                {
                    "type": MsgType.FAULT_ACK.value,
                    "device": self.NAME,
                    "request_id": message.get("request_id"),
                    "faults": self.faults.summary(),
                }
            )
        elif kind == MsgType.PAUSE.value:
            self.bus_paused = True
            self._rebuild_image()
        elif kind == MsgType.RESUME.value:
            self.bus_paused = False
            self._last_tick_wall = time.monotonic()
            self._rebuild_image()

    async def _handle_tick(self, message: dict) -> None:
        self.tick = int(message.get("tick", self.tick + 1))
        self.sim_time = float(message.get("sim_time", self.sim_time))
        dt = float(message.get("dt", self.dt))
        self.dt = dt
        self.bus_ok = True
        self.bus_paused = False
        self._last_tick_wall = time.monotonic()
        raw_inputs = message.get("inputs") or {}
        self.inputs = {name: SignalValue.from_dict(data) for name, data in raw_inputs.items()}
        self.scan(dt)
        await self.bus.send(
            {
                "type": MsgType.TICK_DONE.value,
                "device": self.NAME,
                "tick": self.tick,
                "outputs": self.publish(),
                "quality": self.overall_quality().name,
                "state": int(self.sm.state),
                "tripped": self.sm.tripped or self.protection.any_latched,
            }
        )

    def _handle_fault(self, message: dict) -> None:
        if not self.lab_mode:
            self._emit("FAULT_INJECT_REJECTED", reason="LAB_MODE 未開啟")
            return
        payload = message.get("payload") or {}
        action = payload.get("action", "set")
        if action == "clear":
            category = payload.get("category")
            name = payload.get("name")
            # 只有「全部清除」或明確指定 comm 時才重置協定層故障；
            # 清單一 sensor/process 故障不應把通訊故障一併清掉
            if category in (None, "comm"):
                self.comm_faults.reset()
            if category != "comm":
                try:
                    self.faults.clear(category, name)
                except ValueError as exc:
                    # 未知類別必須回報，不可讓例外沿著匯流排迴圈冒泡把設備打掛
                    self._emit("FAULT_CLEAR_FAILED", error=repr(exc), **payload)
                    return
            self._emit("FAULT_CLEARED", **payload)
            return
        category = payload.get("category")
        name = payload.get("name")
        spec = payload.get("spec")
        if category == "comm":
            for key, value in (spec or {}).items():
                if hasattr(self.comm_faults, key):
                    setattr(self.comm_faults, key, value)
            self._emit("COMM_FAULT_SET", spec=spec)
            return
        try:
            self.faults.set(category, name, spec)
            self._emit("FAULT_SET", category=category, name=name, spec=spec)
        except Exception as exc:
            self._emit("FAULT_SET_FAILED", error=repr(exc), payload=payload)

    async def shutdown(self) -> None:
        """停止主迴圈與背景工作並保存狀態（測試與優雅關機用）。"""
        self._running = False
        for task in self._background:
            task.cancel()
        self._background.clear()
        await self.server.stop()
        await self.bus.close()
        try:
            self.store.save(self._persist_payload())
        finally:
            self.store.close()
            self.log.close()

    async def _persist_loop(self) -> None:
        period = float(cfg_get(self.cfg, "persistence.period", 1.0))
        while True:
            await asyncio.sleep(period)
            try:
                self.store.save(self._persist_payload())
            except Exception as exc:  # pragma: no cover
                self.log.emit("PERSIST_ERROR", error=repr(exc))


def run_device(device_class: type[BaseDevice]) -> None:
    """設備容器進入點。"""
    device = device_class(
        config_dir=os.environ.get("CONFIG_DIR", "/app/configs"),
        state_dir=os.environ.get("STATE_DIR", "/var/lib/plant-device"),
    )
    try:
        asyncio.run(device.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        device.log.emit("SHUTDOWN")
        try:
            asyncio.run(device.shutdown())
        except Exception:
            device.store.close()
            device.log.close()
