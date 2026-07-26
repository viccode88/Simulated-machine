"""簡化 DCS／PLC 控制器。

只透過 Modbus TCP（control_net）操作設備，不使用 sim_net 修改物理量；
另以 CONTROLLER 身分連上 plant-bus，僅為了時間同步、事件與快照參與。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from pymodbus.client import AsyncModbusTcpClient

from common.modbus.register_map import RESET_KEY_VALUE, RegisterMap, Table
from common.simbus.client import SimBusClient
from common.simbus.protocol import DEFAULT_BUS_PORT, MsgType, Role
from common.util import EventLogger, cfg_get, clamp, env_bool, install_excepthook, load_config
from controller.pid import PID, ThreeElementLevel
from controller.startup_sequence import Sequencer
from controller.trip_matrix import TripMatrix

from devices.boiler.main import Boiler
from devices.condensate_pump.main import CondensatePump
from devices.condenser.main import Condenser
from devices.feedwater_pump.main import FeedwaterPump
from devices.feedwater_tank.main import FeedwaterTank
from devices.generator.main import Generator
from devices.steam_valve.main import SteamValve
from devices.turbine.main import Turbine

DEVICE_CLASSES = {
    "condenser": Condenser,
    "condensate_pump": CondensatePump,
    "feedwater_tank": FeedwaterTank,
    "feedwater_pump": FeedwaterPump,
    "boiler": Boiler,
    "steam_valve": SteamValve,
    "turbine": Turbine,
    "generator": Generator,
}


def build_map(name: str) -> RegisterMap:
    klass = DEVICE_CLASSES[name]
    return RegisterMap.build(name, klass.PROCESS_INPUTS, klass.EXTRA_HOLDINGS, klass.EXTRA_COILS)


class DeviceLink:
    """單一設備的 Modbus 連線與資料快取。"""

    def __init__(self, name: str, host: str, port: int, unit: int, log: EventLogger) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.unit = unit
        self.log = log
        self.rmap = build_map(name)
        # pymodbus 的 AsyncModbusTcpClient 在建構時就會呼叫 asyncio.get_running_loop()，
        # 而 DCS 物件是在 asyncio.run() 之前建立的，因此連線物件必須延後到 connect() 才建。
        self.client: AsyncModbusTcpClient | None = None
        self.inputs: list[int] = [0] * 64
        self.discretes: list[bool] = [False] * 16
        self.holdings: list[int] = [0] * 32
        self.online = False
        self.error_count = 0
        self.watchdog = 0
        self.command_sequence = 1
        self.last_poll = 0.0

    async def connect(self) -> None:
        try:
            if self.client is None:
                self.client = AsyncModbusTcpClient(self.host, port=self.port,
                                                   timeout=1.0, retries=1)
            await self.client.connect()
        except Exception as exc:
            self.log.emit("MODBUS_CONNECT_FAILED", target=self.name, error=repr(exc))

    async def poll(self) -> bool:
        if self.client is None:
            await self.connect()
        if self.client is None:
            return False
        try:
            result = await self.client.read_input_registers(0, count=50, device_id=self.unit)
            if result.isError():
                raise IOError(str(result))
            self.inputs[: len(result.registers)] = result.registers
            bits = await self.client.read_discrete_inputs(0, count=16, device_id=self.unit)
            if not bits.isError():
                self.discretes = list(bits.bits)[:16]
            holdings = await self.client.read_holding_registers(0, count=32, device_id=self.unit)
            if not holdings.isError():
                self.holdings[: len(holdings.registers)] = holdings.registers
            self.online = True
            self.last_poll = time.monotonic()
            return True
        except Exception as exc:
            self.error_count += 1
            if self.online:
                self.log.emit("MODBUS_POLL_FAILED", target=self.name, error=repr(exc))
            self.online = False
            return False

    # -- 解碼 --------------------------------------------------------------
    def ir(self, name: str) -> float:
        spec = self.rmap.by_name(Table.INPUT, name)
        raw = self.inputs[spec.offset]
        if spec.dtype == "i16" and raw >= 0x8000:
            raw -= 0x10000
        if spec.dtype == "u32":
            raw = (self.inputs[spec.offset] << 16) | self.inputs[spec.offset + 1]
        return raw / spec.scale

    def di(self, name: str) -> bool:
        spec = self.rmap.by_name(Table.DISCRETE, name)
        return bool(self.discretes[spec.offset])

    def hr(self, name: str) -> float:
        spec = self.rmap.by_name(Table.HOLDING, name)
        return self.holdings[spec.offset] / spec.scale

    # -- 寫入 --------------------------------------------------------------
    async def write_hr(self, name: str, value: float) -> bool:
        if self.client is None:
            return False
        spec = self.rmap.by_name(Table.HOLDING, name)
        raw = int(round(value * spec.scale))
        raw = max(0, min(0xFFFF, raw))
        try:
            result = await self.client.write_register(spec.offset, raw, device_id=self.unit)
            if result.isError():
                self.log.emit("MODBUS_WRITE_REJECTED", target=self.name, register=name,
                              value=value, response=str(result))
                return False
            return True
        except Exception as exc:
            self.error_count += 1
            self.log.emit("MODBUS_WRITE_FAILED", target=self.name, register=name, error=repr(exc))
            return False

    async def pulse_coil(self, name: str) -> bool:
        if self.client is None:
            return False
        try:
            spec = self.rmap.by_name(Table.COIL, name)
        except KeyError:
            return False
        try:
            result = await self.client.write_coil(spec.offset, True, device_id=self.unit)
            return not result.isError()
        except Exception as exc:
            self.error_count += 1
            self.log.emit("MODBUS_COIL_FAILED", target=self.name, coil=name, error=repr(exc))
            return False

    async def kick_watchdog(self) -> None:
        self.watchdog = (self.watchdog + 1) % 65535 or 1
        await self.write_hr("WATCHDOG_COUNTER", self.watchdog)

    async def reset_trip(self) -> bool:
        self.command_sequence = (self.command_sequence + 1) % 65535 or 1
        await self.write_hr("RESET_KEY", RESET_KEY_VALUE)
        await self.write_hr("COMMAND_SEQUENCE", self.command_sequence)
        return await self.pulse_coil("RESET_TRIP")


class DCS:
    def __init__(self, config_dir: str = "/app/configs") -> None:
        self.cfg = load_config(os.path.join(config_dir, "plant.yaml"),
                               os.path.join(config_dir, "dcs.yaml"))
        state_dir = os.environ.get("STATE_DIR", "/var/lib/plant-device")
        os.makedirs(state_dir, exist_ok=True)
        self.log = EventLogger(device="dcs-plc", path=os.path.join(state_dir, "events.jsonl"))
        install_excepthook(self.log)

        self.devices: dict[str, DeviceLink] = {}
        for name in DEVICE_CLASSES:
            host = str(cfg_get(self.cfg, f"dcs.hosts.{name}", name.replace("_", "-")))
            port = int(cfg_get(self.cfg, "dcs.port", 502))
            unit = int(cfg_get(self.cfg, "modbus.unit_id", 1))
            self.devices[name] = DeviceLink(name, host, port, unit, self.log)

        self.min_turbine_pressure = float(cfg_get(self.cfg, "boiler.min_turbine_pressure_bar", 30.0))
        self.target_load_mw = float(cfg_get(self.cfg, "dcs.target_load_mw", 60.0))
        self.rated_flow = float(cfg_get(self.cfg, "feedwater_pump.rated_flow_kg_s", 120.0))
        self.auto_start = env_bool("AUTO_START", bool(cfg_get(self.cfg, "dcs.auto_start", True)))
        self.startup_burner_max = float(cfg_get(self.cfg, "dcs.startup_burner_max_pct", 20.0))
        self.startup_valve_max = float(cfg_get(self.cfg, "dcs.startup_valve_max_pct", 15.0))
        self._pressure_ramp_done = False
        self.scan_time = float(cfg_get(self.cfg, "dcs.pid_scan_s", 0.5))

        loops = self.cfg.get("dcs", {}).get("loops", {})
        self.boiler_pressure = self._pid("boiler_pressure", loops, setpoint=100.0,
                                         rate_up=5.0, rate_down=10.0)
        level_cfg = loops.get("boiler_level", {})
        self.boiler_level = ThreeElementLevel(
            level_pid=self._pid("boiler_level", loops, setpoint=66.7, out_min=-50.0, out_max=50.0),
            flow_pid=PID(
                name="boiler_feedwater_flow",
                kp=float(level_cfg.get("flow_kp", 1.2)),
                ki=float(level_cfg.get("flow_ki", 0.4)),
                kd=0.0,
                out_min=0.0, out_max=100.0, rate_up=25.0, rate_down=25.0,
                integral_limit=100.0,
            ),
            feedforward_gain=float(level_cfg.get("feedforward_gain", 1.0)),
            enabled=bool(level_cfg.get("three_element", True)),
        )
        self.tank_level = self._pid("tank_level", loops, setpoint=60.0, rate_up=3.0, rate_down=3.0)
        self.turbine_speed = self._pid("turbine_speed", loops, setpoint=3000.0,
                                       rate_up=20.0, rate_down=40.0)
        self.load_control = self._pid("load_control", loops, setpoint=0.0,
                                      rate_up=10.0, rate_down=20.0)

        self.sequencer = Sequencer()
        self.trip_matrix = TripMatrix(emit=self.emit)
        self.mode = "AUTO"
        self.sim_time = 0.0
        self.bus = SimBusClient(
            "dcs-plc",
            os.environ.get("SIM_BUS_HOST", str(cfg_get(self.cfg, "simulation.bus_host", "plant-bus"))),
            int(os.environ.get("SIM_BUS_PORT", cfg_get(self.cfg, "simulation.bus_port",
                                                      DEFAULT_BUS_PORT))),
            role=Role.CONTROLLER,
        )
        self.paused = False

    def _pid(self, name: str, loops: dict, setpoint: float, out_min: float = 0.0,
             out_max: float = 100.0, rate_up: float = 100.0, rate_down: float = 100.0) -> PID:
        c = loops.get(name, {})
        return PID(
            name=name,
            kp=float(c.get("kp", 1.0)),
            ki=float(c.get("ki", 0.1)),
            kd=float(c.get("kd", 0.0)),
            setpoint=float(c.get("setpoint", setpoint)),
            out_min=float(c.get("out_min", out_min)),
            out_max=float(c.get("out_max", out_max)),
            rate_up=float(c.get("rate_up", rate_up)),
            rate_down=float(c.get("rate_down", rate_down)),
            deadband=float(c.get("deadband", 0.0)),
            integral_limit=float(c.get("integral_limit", 100.0)),
            direct_acting=bool(c.get("direct_acting", True)),
        )

    # -- 便利存取（給 sequencer 用） ----------------------------------------
    def emit(self, event: str, **fields: Any) -> None:
        record = self.log.emit(event, **fields)
        if self.bus.connected.is_set():
            asyncio.ensure_future(self.bus.send_event(record))

    def pv(self, device: str, register: str) -> float:
        try:
            return self.devices[device].ir(register)
        except Exception:
            return 0.0

    def di(self, device: str, register: str) -> bool:
        try:
            return self.devices[device].di(register)
        except Exception:
            return False

    async def write(self, device: str, register: str, value: float) -> bool:
        return await self.devices[device].write_hr(register, value)

    async def pulse(self, device: str, coil: str) -> bool:
        return await self.devices[device].pulse_coil(coil)

    # -- 主要迴圈 ----------------------------------------------------------
    async def run(self) -> None:
        for link in self.devices.values():
            await link.connect()
        self.bus.start()
        asyncio.ensure_future(self._bus_loop())
        asyncio.ensure_future(self._poll_loop())
        asyncio.ensure_future(self._watchdog_loop())
        asyncio.ensure_future(self._control_loop())
        asyncio.ensure_future(self._sequence_loop())
        self.emit("DCS_STARTED", devices=sorted(self.devices), auto_start=self.auto_start)
        while True:
            await asyncio.sleep(3600)

    async def _poll_loop(self) -> None:
        period = float(cfg_get(self.cfg, "dcs.poll_s", 0.25))
        while True:
            await asyncio.gather(*[link.poll() for link in self.devices.values()],
                                 return_exceptions=True)
            await asyncio.sleep(period)

    async def _watchdog_loop(self) -> None:
        while True:
            for link in self.devices.values():
                if link.online:
                    await link.kick_watchdog()
            await asyncio.sleep(1.0)

    async def _sequence_loop(self) -> None:
        await asyncio.sleep(float(cfg_get(self.cfg, "dcs.start_delay_s", 8.0)))
        if self.auto_start and not self.sequencer.running:
            self.sequencer.start()
            self.emit("STARTUP_SEQUENCE_STARTED")
        while True:
            if not self.paused:
                try:
                    await self.sequencer.update(self, 1.0)
                except Exception as exc:
                    self.emit("SEQUENCE_ERROR", error=repr(exc))
            await asyncio.sleep(1.0)

    async def _control_loop(self) -> None:
        dt = self.scan_time
        while True:
            started = time.monotonic()
            if not self.paused:
                try:
                    await self._control_step(dt)
                except Exception as exc:
                    self.emit("CONTROL_ERROR", error=repr(exc))
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, dt - elapsed))

    async def _control_step(self, dt: float) -> None:
        tripped = {
            name: link.di("TRIPPED") for name, link in self.devices.items() if link.online
        }
        for action in self.trip_matrix.evaluate(tripped):
            if action.action == "write_holding":
                await self.write(action.device, action.target, action.value)
            elif action.action == "pulse_coil":
                await self.pulse(action.device, action.target)

        # --- 8.1 鍋爐壓力控制 ---
        boiler = self.devices["boiler"]
        if boiler.online and self.boiler_pressure.auto:
            pressure = boiler.ir("BOILER_PRESSURE")
            burner = self.boiler_pressure.update(pressure, dt)
            # 升壓期間限制燃燒器輸出，避免壓力大幅過衝（達壓後鎖存解除）
            if pressure >= self.boiler_pressure.setpoint * 0.95:
                self._pressure_ramp_done = True
            if not self._pressure_ramp_done:
                burner = min(burner, self.startup_burner_max)
            if boiler.di("TRIPPED"):
                self.boiler_pressure.force_output(0.0)
                burner = 0.0
            await boiler.write_hr("MANUAL_OUTPUT", burner)

        # --- 8.2 鍋爐水位控制（三元素） ---
        pump = self.devices["feedwater_pump"]
        if boiler.online and pump.online and self.boiler_level.level_pid.auto:
            speed = self.boiler_level.update(
                boiler.ir("LEVEL_INDICATED"),
                boiler.ir("STEAM_OUTFLOW"),
                boiler.ir("FEEDWATER_FLOW"),
                dt,
                rated_flow=self.rated_flow,
            )
            if boiler.ir("FEEDWATER_PERMITTED") < 1:
                speed = 0.0
                self.boiler_level.flow_pid.force_output(0.0)
            await pump.write_hr("MANUAL_OUTPUT", clamp(speed, 0.0, 100.0))

        # --- 8.3 給水槽水位控制 ---
        tank = self.devices["feedwater_tank"]
        cpump = self.devices["condensate_pump"]
        if tank.online and cpump.online and self.tank_level.auto:
            speed = self.tank_level.update(tank.ir("TANK_LEVEL"), dt)
            await cpump.write_hr("MANUAL_OUTPUT", clamp(speed, 0.0, 100.0))

        # --- 8.4 汽輪機轉速控制 ---
        turbine = self.devices["turbine"]
        valve = self.devices["steam_valve"]
        generator = self.devices["generator"]
        if turbine.online and valve.online and self.turbine_speed.auto:
            speed_rpm = turbine.ir("SPEED_RPM")
            grid_mode = generator.online and generator.ir("OPERATING_MODE") >= 1
            load_ff = 0.0
            if generator.online:
                # 負載前饋：以額定負載換算閥門開度
                load_ff = 0.8 * generator.ir("ELECTRICAL_POWER")
            if grid_mode:
                self.load_control.setpoint = generator.ir("LOAD_DEMAND") if generator.online else 0.0
                position = self.load_control.update(
                    generator.ir("ELECTRICAL_POWER") if generator.online else 0.0, dt
                )
            else:
                position = self.turbine_speed.update(speed_rpm, dt, feedforward=load_ff)

            # 升速期間限制閥門開度，避免瞬間超速
            breaker_closed = generator.online and generator.ir("BREAKER_STATUS") >= 1
            if speed_rpm < self.turbine_speed.setpoint * 0.97 and not breaker_closed:
                position = min(position, self.startup_valve_max)

            # 安全邏輯永遠優先：超速時無條件關閥
            overspeed = float(cfg_get(self.cfg, "dcs.overspeed_close_rpm", 3150.0))
            if speed_rpm > overspeed or turbine.di("TRIPPED"):
                position = 0.0
                self.turbine_speed.force_output(0.0)
                self.load_control.force_output(0.0)
            await valve.write_hr("MANUAL_OUTPUT", clamp(position, 0.0, 100.0))

    async def _bus_loop(self) -> None:
        while True:
            message = await self.bus.next_message(timeout=5.0)
            if message is None:
                continue
            kind = message.get("type")
            if kind == MsgType.TICK.value:
                self.sim_time = float(message.get("sim_time", self.sim_time))
                self.log.sim_time_fn = lambda: self.sim_time
            elif kind == MsgType.PAUSE.value:
                self.paused = True
            elif kind == MsgType.RESUME.value:
                self.paused = False
            elif kind == MsgType.SNAPSHOT_SAVE.value:
                await self.bus.send({
                    "type": MsgType.SNAPSHOT_DATA.value,
                    "device": "dcs-plc",
                    "request_id": message.get("request_id"),
                    "state": self.snapshot_state(),
                })
            elif kind == MsgType.SNAPSHOT_RESTORE.value:
                ok, error = True, ""
                try:
                    self.restore_state(message.get("state") or {})
                except Exception as exc:
                    ok, error = False, repr(exc)
                await self.bus.send({
                    "type": MsgType.RESTORE_ACK.value,
                    "device": "dcs-plc",
                    "request_id": message.get("request_id"),
                    "ok": ok,
                    "error": error,
                })

    # -- 快照 --------------------------------------------------------------
    def snapshot_state(self) -> dict:
        return {
            "version": 1,
            "device": "dcs-plc",
            "mode": self.mode,
            "target_load_mw": self.target_load_mw,
            "loops": {
                "boiler_pressure": self.boiler_pressure.to_dict(),
                "boiler_level": self.boiler_level.to_dict(),
                "tank_level": self.tank_level.to_dict(),
                "turbine_speed": self.turbine_speed.to_dict(),
                "load_control": self.load_control.to_dict(),
            },
            "pressure_ramp_done": self._pressure_ramp_done,
            "sequencer": self.sequencer.to_dict(),
            "trip_matrix": self.trip_matrix.to_dict(),
        }

    def restore_state(self, data: dict) -> None:
        loops = data.get("loops") or {}
        self.boiler_pressure.from_dict(loops.get("boiler_pressure") or {})
        self.boiler_level.from_dict(loops.get("boiler_level") or {})
        self.tank_level.from_dict(loops.get("tank_level") or {})
        self.turbine_speed.from_dict(loops.get("turbine_speed") or {})
        self.load_control.from_dict(loops.get("load_control") or {})
        self.sequencer.from_dict(data.get("sequencer") or {})
        self.trip_matrix.from_dict(data.get("trip_matrix") or {})
        self._pressure_ramp_done = bool(data.get("pressure_ramp_done", False))
        self.mode = str(data.get("mode", self.mode))
        self.target_load_mw = float(data.get("target_load_mw", self.target_load_mw))
        self.emit("DCS_SNAPSHOT_RESTORED", sequencer_step=self.sequencer.index)


def main() -> None:
    dcs = DCS(config_dir=os.environ.get("CONFIG_DIR", "/app/configs"))
    try:
        asyncio.run(dcs.run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        dcs.log.emit("DCS_SHUTDOWN")
        dcs.log.close()


if __name__ == "__main__":
    main()
