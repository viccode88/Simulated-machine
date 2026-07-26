"""plant-bus：模擬時間同步、程序量路由、品質管理、快照協調。

plant-bus 不含任何物理公式；物理模型全部留在各設備容器內。
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from common.simbus.protocol import MsgType, Role, SignalQuality, decode, encode
from common.util import EventLogger, cfg_get, wall_time_iso

from .snapshot_store import SnapshotIntegrityError


@dataclass
class Participant:
    name: str
    role: str
    writer: Any
    publishes: list[str] = field(default_factory=list)
    subscribes: list[str] = field(default_factory=list)
    last_tick: int = -1
    missed_ticks: int = 0
    total_ticks: int = 0
    connected_at: float = field(default_factory=time.time)
    quality: str = SignalQuality.GOOD.value
    state: int = 0
    tripped: bool = False
    last_latency_ms: float = 0.0

    @property
    def is_device(self) -> bool:
        return self.role == Role.DEVICE.value


@dataclass
class Signal:
    name: str
    value: float = 0.0
    tick: int = -1
    source: str = ""
    forced: bool = False


class PlantBus:
    def __init__(self, cfg: dict, snapshot_store, log: EventLogger) -> None:
        self.cfg = cfg
        self.store = snapshot_store
        self.log = log
        self.dt = float(cfg_get(cfg, "simulation.dt", 0.1))
        self.speed = float(cfg_get(cfg, "simulation.speed", 1.0))
        self.tick_timeout = float(cfg_get(cfg, "simulation.tick_timeout", 0.35))
        self.stale_seconds = float(cfg_get(cfg, "simulation.stale_seconds", 1.0))
        self.bad_seconds = float(cfg_get(cfg, "simulation.bad_seconds", 3.0))
        self.expected_devices: list[str] = list(cfg_get(cfg, "simulation.devices", []) or [])
        self.port = int(cfg_get(cfg, "simulation.bus_port", 7000))

        self.tick = 0
        self.sim_time = 0.0
        self.paused = bool(cfg_get(cfg, "simulation.start_paused", False))
        self.step_budget = 0
        self.participants: dict[str, Participant] = {}
        self.signals: dict[str, Signal] = {}
        self.events: deque[dict] = deque(maxlen=int(cfg_get(cfg, "bus.event_ring", 2000)))
        self.snapshot_generation = 0
        self.last_snapshot: str | None = None
        self.auto_snapshot_name = str(cfg_get(cfg, "snapshot.auto_name", "autosave"))
        self.auto_snapshot_period = float(cfg_get(cfg, "snapshot.auto_period", 0.0))
        self.busy_reason: str | None = None

        self._tick_waiters: dict[int, dict[str, Any]] = {}
        self._request_waiters: dict[str, dict[str, Any]] = {}
        self._snapshot_lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._tick_idle = asyncio.Event()   # set = 沒有 tick 正在進行
        self._tick_idle.set()
        self._real_time_start = time.monotonic()

    # ------------------------------------------------------------------
    # 連線處理
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, "0.0.0.0", self.port)
        self.log.emit("BUS_LISTENING", port=self.port, dt=self.dt,
                      expected_devices=self.expected_devices)
        self._tasks.append(asyncio.ensure_future(self._tick_loop()))
        if self.auto_snapshot_period > 0:
            self._tasks.append(asyncio.ensure_future(self._auto_snapshot_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        for participant in list(self.participants.values()):
            with contextlib.suppress(Exception):
                participant.writer.close()
        self.participants.clear()
        self.log.emit("BUS_STOPPED", tick=self.tick)

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        name = "?"
        try:
            line = await reader.readline()
            if not line:
                return
            hello = decode(line)
            if hello.get("type") != MsgType.HELLO.value:
                writer.close()
                return
            name = str(hello.get("device", "?"))
            participant = Participant(
                name=name,
                role=str(hello.get("role", Role.DEVICE.value)),
                writer=writer,
                publishes=list(hello.get("publishes") or []),
                subscribes=list(hello.get("subscribes") or []),
            )
            old = self.participants.get(name)
            if old is not None:
                with contextlib.suppress(Exception):
                    old.writer.close()
            self.participants[name] = participant
            for signal_name in participant.publishes:
                self.signals.setdefault(signal_name, Signal(signal_name, source=name))
            self._record_event(
                {"device": "plant-bus", "event": "PARTICIPANT_JOINED", "name": name,
                 "role": participant.role, "publishes": participant.publishes}
            )
            await self._send(participant, {
                "type": MsgType.WELCOME.value,
                "tick": self.tick,
                "sim_time": self.sim_time,
                "dt": self.dt,
                "paused": self.paused,
            })
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = decode(line)
                except Exception:
                    continue
                await self._on_message(participant, message)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            self.log.emit("BUS_CONN_ERROR", name=name, error=repr(exc))
        finally:
            if self.participants.get(name) and self.participants[name].writer is writer:
                del self.participants[name]
                self._record_event({"device": "plant-bus", "event": "PARTICIPANT_LEFT", "name": name})
            with contextlib.suppress(Exception):
                writer.close()

    async def _send(self, participant: Participant, message: dict) -> bool:
        try:
            participant.writer.write(encode(message))
            await participant.writer.drain()
            return True
        except Exception:
            return False

    async def broadcast(self, message: dict, roles: tuple[str, ...] = (Role.DEVICE.value,)) -> None:
        await asyncio.gather(
            *[self._send(p, message) for p in list(self.participants.values()) if p.role in roles],
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # 訊息處理
    # ------------------------------------------------------------------
    async def _on_message(self, participant: Participant, message: dict) -> None:
        kind = message.get("type")
        if kind == MsgType.TICK_DONE.value:
            self._on_tick_done(participant, message)
        elif kind == MsgType.EVENT.value:
            payload = message.get("payload") or {}
            self._record_event(payload)
            await self.broadcast({"type": MsgType.EVENT.value, "device": participant.name,
                                  "payload": payload}, roles=(Role.OBSERVER.value,))
        elif kind in (MsgType.SNAPSHOT_DATA.value, MsgType.RESTORE_ACK.value, MsgType.FAULT_ACK.value):
            self._resolve_request(participant.name, message)

    def _on_tick_done(self, participant: Participant, message: dict) -> None:
        tick = int(message.get("tick", -1))
        participant.last_tick = tick
        participant.total_ticks += 1
        participant.quality = str(message.get("quality", SignalQuality.GOOD.value))
        participant.state = int(message.get("state", 0))
        participant.tripped = bool(message.get("tripped", False))
        for name, value in (message.get("outputs") or {}).items():
            signal = self.signals.setdefault(name, Signal(name, source=participant.name))
            if signal.forced:
                continue
            signal.value = float(value)
            signal.tick = tick
            signal.source = participant.name
        waiter = self._tick_waiters.get(tick)
        if waiter and participant.name in waiter["expected"]:
            waiter["received"].add(participant.name)
            if waiter["expected"] <= waiter["received"]:
                waiter["event"].set()

    def _resolve_request(self, device: str, message: dict) -> None:
        request_id = str(message.get("request_id", ""))
        waiter = self._request_waiters.get(request_id)
        if not waiter:
            return
        waiter["results"][device] = message
        if set(waiter["expected"]) <= set(waiter["results"]):
            waiter["event"].set()

    def _record_event(self, payload: dict) -> None:
        payload.setdefault("wall_time", wall_time_iso())
        payload.setdefault("sim_time", round(self.sim_time, 3))
        self.events.append(payload)

    # ------------------------------------------------------------------
    # Tick 迴圈（lockstep）
    # ------------------------------------------------------------------
    def _quality_for(self, signal: Signal) -> str:
        if signal.forced:
            return SignalQuality.GOOD.value
        if signal.tick < 0:
            return SignalQuality.BAD.value
        age = (self.tick - signal.tick) * self.dt
        if age <= self.dt * 1.5:
            return SignalQuality.GOOD.value
        if age <= self.stale_seconds:
            return SignalQuality.STALE.value
        if age <= self.bad_seconds:
            return SignalQuality.STALE.value
        return SignalQuality.BAD.value

    def _inputs_for(self, participant: Participant) -> dict[str, dict]:
        payload: dict[str, dict] = {}
        for name in participant.subscribes:
            signal = self.signals.get(name)
            if signal is None:
                payload[name] = {"value": 0.0, "quality": SignalQuality.BAD.value, "tick": -1,
                                 "source": ""}
            else:
                payload[name] = {
                    "value": signal.value,
                    "quality": self._quality_for(signal),
                    "tick": signal.tick,
                    "source": signal.source,
                }
        return payload

    async def _tick_loop(self) -> None:
        while True:
            if self.paused and self.step_budget <= 0:
                await asyncio.sleep(0.02)
                continue
            started = time.monotonic()
            await self._run_one_tick()
            if self.step_budget > 0:
                self.step_budget -= 1
                if self.step_budget == 0:
                    # 走 pause()：必須廣播 PAUSE，否則設備會自行 free-run
                    # 而 plant-bus 仍顯示 paused（暫停狀態分裂）
                    await self.pause(reason="step_complete")
                    continue
            elapsed = time.monotonic() - started
            target = self.dt / max(0.01, self.speed)
            if elapsed < target:
                await asyncio.sleep(target - elapsed)

    async def _run_one_tick(self) -> None:
        self._tick_idle.clear()
        try:
            await self._do_tick()
        finally:
            self._tick_idle.set()

    async def _do_tick(self) -> None:
        self.tick += 1
        self.sim_time = round(self.sim_time + self.dt, 6)
        devices = [p for p in self.participants.values() if p.is_device]
        expected = {p.name for p in devices}
        waiter = {"expected": expected, "received": set(), "event": asyncio.Event()}
        self._tick_waiters[self.tick] = waiter
        started = time.monotonic()
        await asyncio.gather(
            *[
                self._send(p, {
                    "type": MsgType.TICK.value,
                    "tick": self.tick,
                    "sim_time": self.sim_time,
                    "dt": self.dt,
                    "inputs": self._inputs_for(p),
                })
                for p in devices
            ],
            return_exceptions=True,
        )
        if expected:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter["event"].wait(), timeout=self.tick_timeout)
        latency = (time.monotonic() - started) * 1000.0
        missing = expected - waiter["received"]
        for name in missing:
            participant = self.participants.get(name)
            if participant:
                participant.missed_ticks += 1
                if participant.missed_ticks in (1, 10, 100) or participant.missed_ticks % 100 == 0:
                    self._record_event({
                        "device": "plant-bus", "event": "DEVICE_TICK_TIMEOUT", "name": name,
                        "tick": self.tick, "missed_ticks": participant.missed_ticks,
                    })
        for name in waiter["received"]:
            participant = self.participants.get(name)
            if participant:
                participant.missed_ticks = 0
                participant.last_latency_ms = latency
        del self._tick_waiters[self.tick]
        # 觀察者（historian / HMI）取得完整程序影像；
        # 控制器一定要收到 tick——它的 PID 掃描與 watchdog 都靠這個時間基準，
        # 不能因為沒有 observer 連線就收不到（否則 DCS 會失去模擬時間）
        listeners = tuple(
            role for role in (Role.OBSERVER.value, Role.CONTROLLER.value)
            if any(p.role == role for p in self.participants.values())
        )
        if listeners:
            await self.broadcast(
                {
                    "type": MsgType.TICK.value,
                    "tick": self.tick,
                    "sim_time": self.sim_time,
                    "dt": self.dt,
                    "inputs": {
                        name: {"value": s.value, "quality": self._quality_for(s), "tick": s.tick,
                               "source": s.source}
                        for name, s in self.signals.items()
                    },
                },
                roles=listeners,
            )

    # ------------------------------------------------------------------
    # 模擬控制
    # ------------------------------------------------------------------
    async def pause(self, reason: str = "manual") -> None:
        if not self.paused:
            self.paused = True
            self.step_budget = 0
            await self.broadcast({"type": MsgType.PAUSE.value, "reason": reason},
                                 roles=(Role.DEVICE.value, Role.CONTROLLER.value, Role.OBSERVER.value))
            self._record_event({"device": "plant-bus", "event": "SIM_PAUSED", "reason": reason})
        # 在 tick 邊界暫停：等待進行中的 tick 完成，確保快照是一致的時間切片
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._tick_idle.wait(), timeout=self.tick_timeout + 0.5)

    async def resume(self, reason: str = "manual") -> None:
        if self.paused:
            self.paused = False
            self.step_budget = 0
            await self.broadcast({"type": MsgType.RESUME.value, "reason": reason},
                                 roles=(Role.DEVICE.value, Role.CONTROLLER.value, Role.OBSERVER.value))
            self._record_event({"device": "plant-bus", "event": "SIM_RESUMED", "reason": reason})

    async def step(self, ticks: int = 1) -> None:
        """單步執行 N 個 tick。

        必須廣播 RESUME／PAUSE，讓設備與 plant-bus 的暫停狀態保持一致；
        否則 `pause; step N` 之後設備會以為模擬仍在執行而自行 free-run。
        """
        count = max(1, int(ticks))
        # 先設定預算再解除暫停，避免 tick loop 在中間看到「未暫停且無預算」而失控執行
        self.step_budget = count
        was_paused = self.paused
        self.paused = False
        if was_paused:
            await self.broadcast(
                {"type": MsgType.RESUME.value, "reason": "step"},
                roles=(Role.DEVICE.value, Role.CONTROLLER.value, Role.OBSERVER.value),
            )
        self._record_event({"device": "plant-bus", "event": "SIM_STEP", "ticks": count,
                            "tick": self.tick})

    def set_speed(self, factor: float) -> None:
        self.speed = max(0.01, min(50.0, float(factor)))
        self._record_event({"device": "plant-bus", "event": "SIM_SPEED", "speed": self.speed})

    # ------------------------------------------------------------------
    # 請求/回應（快照、故障注入）
    # ------------------------------------------------------------------
    async def _request(self, message: dict, expected: list[str], timeout: float = 5.0,
                       roles: tuple[str, ...] = (Role.DEVICE.value, Role.CONTROLLER.value),
                       broadcast: bool | None = None) -> dict:
        """對 expected 送出請求並等待回應。

        broadcast=None 時，只有「目標不只一個」才廣播。單一目標一律點對點傳送；
        目標不存在時直接回傳空結果，絕不可退回廣播（否則打錯字的故障目標會打到全廠）。
        """
        request_id = uuid.uuid4().hex
        message = dict(message, request_id=request_id)
        waiter = {"expected": list(expected), "results": {}, "event": asyncio.Event()}
        self._request_waiters[request_id] = waiter
        try:
            if broadcast is None:
                broadcast = len(expected) != 1
            if not broadcast:
                participant = self.participants.get(expected[0])
                if participant is None:
                    return {}
                await self._send(participant, message)
            else:
                await self.broadcast(message, roles=roles)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter["event"].wait(), timeout=timeout)
            return dict(waiter["results"])
        finally:
            self._request_waiters.pop(request_id, None)

    def _snapshot_participants(self) -> list[str]:
        return [p.name for p in self.participants.values()
                if p.role in (Role.DEVICE.value, Role.CONTROLLER.value)]

    def bus_state(self) -> dict:
        return {
            "tick": self.tick,
            "sim_time": self.sim_time,
            "dt": self.dt,
            "speed": self.speed,
            "signals": {
                name: {"value": s.value, "tick": s.tick, "source": s.source, "forced": s.forced}
                for name, s in self.signals.items()
            },
        }

    # -- 儲存快照 ----------------------------------------------------------
    async def save_snapshot(self, name: str, description: str = "", tags: list[str] | None = None,
                            timeout: float = 5.0) -> dict:
        async with self._snapshot_lock:
            was_paused = self.paused
            self.busy_reason = "SNAPSHOT_SAVE"
            try:
                await self.pause(reason="snapshot_save")
                expected = self._snapshot_participants()
                results = await self._request({"type": MsgType.SNAPSHOT_SAVE.value}, expected, timeout)
                participants = {
                    device: message.get("state")
                    for device, message in results.items()
                    if message.get("state")
                }
                missing = sorted(set(expected) - set(participants))
                meta = self.store.save(name, self.bus_state(), participants, description, tags,
                                       missing=missing)
                if missing:
                    # 不完整的快照不得成為預設還原目標，否則後續 restore 會做出混合機組
                    self._record_event({"device": "plant-bus", "event": "SNAPSHOT_INCOMPLETE",
                                        "name": name, "missing": missing,
                                        "devices": meta["devices"]})
                else:
                    self.last_snapshot = name
                self._record_event({"device": "plant-bus", "event": "SNAPSHOT_SAVED", "name": name,
                                    "devices": meta["devices"], "missing": missing,
                                    "complete": meta["complete"],
                                    "sim_time": self.sim_time, "tick": self.tick})
                return meta
            finally:
                self.busy_reason = None
                if not was_paused:
                    await self.resume(reason="snapshot_save_done")

    # -- 還原快照 ----------------------------------------------------------
    async def restore_snapshot(self, name: str, options: dict | None = None,
                               timeout: float = 5.0) -> dict:
        options = options or {}
        async with self._snapshot_lock:
            # 還原前先驗格式與 checksum：損毀的快照絕不可套用到機組上
            document = self.store.load(name, verify=True)
            meta = document.get("meta") or {}
            if not meta.get("complete", True) and not options.get("allow_incomplete", False):
                raise SnapshotIntegrityError(
                    f"快照 {name} 不完整（缺少 {', '.join(meta.get('missing') or [])}），"
                    "還原會造成部分設備新狀態、部分設備舊狀態；"
                    "如確定要繼續請指定 allow_incomplete=true"
                )
            was_paused = self.paused
            self.busy_reason = "SNAPSHOT_RESTORE"
            started = time.monotonic()
            try:
                await self.pause(reason="snapshot_restore")
                participants = document.get("participants") or {}
                targets = [n for n in self._snapshot_participants() if n in participants]
                results: dict[str, Any] = {}
                for device in targets:
                    result = await self._request(
                        {
                            "type": MsgType.SNAPSHOT_RESTORE.value,
                            "state": participants[device],
                            "options": options,
                            "sim_time": document.get("bus", {}).get("sim_time", 0.0),
                            "tick": document.get("bus", {}).get("tick", 0),
                        },
                        [device],
                        timeout,
                    )
                    results.update(result)

                bus_state = document.get("bus") or {}
                self.tick = int(bus_state.get("tick", self.tick))
                self.sim_time = float(bus_state.get("sim_time", self.sim_time))
                self.signals = {
                    signal_name: Signal(
                        signal_name,
                        value=float(data.get("value", 0.0)),
                        tick=int(data.get("tick", -1)),
                        source=str(data.get("source", "")),
                        forced=bool(data.get("forced", False)),
                    )
                    for signal_name, data in (bus_state.get("signals") or {}).items()
                }
                self.snapshot_generation += 1
                elapsed_ms = (time.monotonic() - started) * 1000.0
                failed = [d for d, r in results.items() if not r.get("ok", False)]
                missing = sorted(set(participants) - set(results))
                summary = {
                    "name": name,
                    "restored": sorted(results.keys()),
                    "failed": failed,
                    "missing": missing,
                    "sim_time": self.sim_time,
                    "tick": self.tick,
                    "generation": self.snapshot_generation,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "options": options,
                }
                self._record_event({"device": "plant-bus", "event": "SNAPSHOT_RESTORED", **summary})
                return summary
            finally:
                self.busy_reason = None
                if options.get("resume", not was_paused):
                    await self.resume(reason="snapshot_restore_done")

    async def _auto_snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.auto_snapshot_period)
            if self.paused:
                continue
            try:
                await self.save_snapshot(self.auto_snapshot_name, description="自動快照",
                                         tags=["auto"])
            except Exception as exc:  # pragma: no cover
                self.log.emit("AUTO_SNAPSHOT_FAILED", error=repr(exc))

    # -- 故障注入路由 ------------------------------------------------------
    async def inject_fault(self, target: str, payload: dict, timeout: float = 3.0) -> dict:
        broadcast = target in ("*", "all")
        if broadcast:
            expected = [p.name for p in self.participants.values() if p.is_device]
        else:
            # 未知目標必須明確失敗；打錯字絕不可退化成全廠廣播
            if target not in self.participants:
                known = sorted(p.name for p in self.participants.values() if p.is_device)
                self._record_event({"device": "plant-bus", "event": "FAULT_TARGET_UNKNOWN",
                                    "target": target, "known": known})
                return {"target": target, "acked": [], "results": {},
                        "error": f"未知的故障目標 {target}", "known_targets": known}
            expected = [target]
        results = await self._request({"type": MsgType.FAULT.value, "payload": payload},
                                      expected, timeout, broadcast=broadcast)
        self._record_event({"device": "plant-bus", "event": "FAULT_INJECTED", "target": target,
                            "payload": payload, "acked": sorted(results.keys())})
        return {"target": target, "acked": sorted(results.keys()),
                "results": {k: v.get("faults") for k, v in results.items()}}

    def force_signal(self, name: str, value: float | None) -> dict:
        signal = self.signals.setdefault(name, Signal(name, source="forced"))
        if value is None:
            signal.forced = False
        else:
            signal.forced = True
            signal.value = float(value)
            signal.tick = self.tick
            signal.source = "forced"
        self._record_event({"device": "plant-bus", "event": "SIGNAL_FORCED", "name": name,
                            "value": value})
        return {"name": name, "value": signal.value, "forced": signal.forced}

    # ------------------------------------------------------------------
    # 狀態輸出
    # ------------------------------------------------------------------
    def state(self) -> dict:
        return {
            "tick": self.tick,
            "sim_time": round(self.sim_time, 3),
            "dt": self.dt,
            "speed": self.speed,
            "paused": self.paused,
            "busy": self.busy_reason,
            "wall_time": wall_time_iso(),
            "uptime": round(time.monotonic() - self._real_time_start, 1),
            "snapshot_generation": self.snapshot_generation,
            "last_snapshot": self.last_snapshot,
            "expected_devices": self.expected_devices,
            "participants": {
                name: {
                    "role": p.role,
                    "last_tick": p.last_tick,
                    "missed_ticks": p.missed_ticks,
                    "quality": p.quality,
                    "state": p.state,
                    "tripped": p.tripped,
                    "publishes": p.publishes,
                    "latency_ms": round(p.last_latency_ms, 2),
                    "online": True,
                }
                for name, p in self.participants.items()
            },
            "offline_devices": [d for d in self.expected_devices if d not in self.participants],
            "signals": {
                name: {"value": round(s.value, 4), "quality": self._quality_for(s),
                       "tick": s.tick, "source": s.source, "forced": s.forced}
                for name, s in sorted(self.signals.items())
            },
        }

    def metrics(self) -> str:
        lines = [
            "# HELP plant_sim_time 模擬時間（秒）",
            "# TYPE plant_sim_time gauge",
            f"plant_sim_time {self.sim_time}",
            "# HELP plant_tick 模擬 tick",
            "# TYPE plant_tick counter",
            f"plant_tick {self.tick}",
            "# HELP plant_paused 模擬是否暫停",
            "# TYPE plant_paused gauge",
            f"plant_paused {1 if self.paused else 0}",
        ]
        lines.append("# HELP plant_signal 程序量")
        lines.append("# TYPE plant_signal gauge")
        for name, signal in sorted(self.signals.items()):
            lines.append(f'plant_signal{{signal="{name}",source="{signal.source}"}} {signal.value}')
        lines.append("# HELP plant_device_missed_ticks 設備逾時次數")
        lines.append("# TYPE plant_device_missed_ticks gauge")
        for name, participant in self.participants.items():
            lines.append(f'plant_device_missed_ticks{{device="{name}"}} {participant.missed_ticks}')
            lines.append(f'plant_device_tripped{{device="{name}"}} {1 if participant.tripped else 0}')
        return "\n".join(lines) + "\n"
