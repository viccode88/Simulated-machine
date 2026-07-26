"""回歸測試：針對一次程式碼審查中確認的 16 個缺陷，鎖住修正後的行為。

每個測試都對應一個曾經可重現的缺陷，命名為 test_<主題>，並在 docstring 標註
原始症狀，避免日後改動時再度回歸。
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common.device.faults import FaultInjector
from common.modbus.register_map import RegisterMap, Table
from common.modbus.server import AccessPolicy, ModbusTcpServer, RegisterImage
from common.simbus.protocol import MsgType, Role
from common.util import EventLogger
from controller.trip_matrix import TripMatrix
from devices.boiler.main import Boiler
from devices.generator.main import Generator
from plant_bus.app.bus import PlantBus, Participant
from plant_bus.app.http_api import _body
from plant_bus.app.snapshot_store import SnapshotIntegrityError, SnapshotStore
from tests.harness import CONFIG_DIR, MiniPlant, bring_to_steady


# ---------------------------------------------------------------------------
# 測試輔助
# ---------------------------------------------------------------------------
class _FakeWriter:
    """收集送往某個參與者的訊息，不需要真的開 TCP 連線。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def write(self, data: bytes) -> None:
        self.sent.append(json.loads(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]


def _bus_with_devices(*names: str, tmp_path=None) -> tuple[PlantBus, dict[str, _FakeWriter]]:
    log = EventLogger(device="plant-bus")
    bus = PlantBus({}, SnapshotStore(str(tmp_path)) if tmp_path else None, log)
    writers: dict[str, _FakeWriter] = {}
    for name in names:
        writers[name] = _FakeWriter()
        bus.participants[name] = Participant(name=name, role=Role.DEVICE.value,
                                             writer=writers[name])
    return bus, writers


# ---------------------------------------------------------------------------
# P1-1 加速模擬會改變 DCS 行為並觸發假通訊逾時
# ---------------------------------------------------------------------------
def _make_dcs():
    from controller.dcs.main import DCS

    os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="dcs-reg-")
    return DCS(config_dir=CONFIG_DIR)


async def test_control_loop_period_follows_simulation_time():
    """症狀：PID／順序／watchdog 依真實時間執行，`speed 5` 時 watchdog 每 5
    模擬秒才更新一次，超過設備 3 模擬秒的逾時門檻而產生假通訊逾時。"""
    dcs = _make_dcs()
    try:
        dcs.bus.connected.set()
        dcs.sim_time = 100.0
        waiter = asyncio.ensure_future(dcs._wait_sim_period(1.0, 100.0))

        await asyncio.sleep(0.25)                 # 真實時間過去，模擬時間沒動
        assert not waiter.done(), "模擬時間未前進時不得提前執行"

        dcs.sim_time = 101.0                      # 模擬時間前進一個週期
        assert await asyncio.wait_for(waiter, timeout=1.0) == pytest.approx(101.0)
    finally:
        dcs.log.close()


async def test_fast_simulation_runs_control_loop_more_often_in_real_time():
    """5 倍速下，1 模擬秒內必須跑滿 1 秒份的掃描次數，不能只跑 1/5。"""
    dcs = _make_dcs()
    try:
        dcs.bus.connected.set()
        dcs.sim_time = 0.0
        last, iterations = 0.0, 0
        # 模擬 plant-bus 以 5 倍速推進：每次迴圈推進 0.1 模擬秒
        for _ in range(10):
            dcs.sim_time = round(dcs.sim_time + 0.1, 6)
            while dcs.sim_time - last >= 0.5:
                last = await dcs._wait_sim_period(0.5, last)
                iterations += 1
        assert iterations == 2, f"1 模擬秒 / 0.5 秒掃描 = 2 次，實際 {iterations}"
    finally:
        dcs.log.close()


async def test_wait_realigns_after_snapshot_rewinds_simulation_time():
    """快照還原把模擬時間往回拉時，控制迴圈不得枯等追上舊時間線。"""
    dcs = _make_dcs()
    try:
        dcs.bus.connected.set()
        dcs.sim_time = 20.0
        waiter = asyncio.ensure_future(dcs._wait_sim_period(1.0, 500.0))
        await asyncio.sleep(0.15)
        dcs.sim_time = 21.0
        assert await asyncio.wait_for(waiter, timeout=1.0) == pytest.approx(21.0)
    finally:
        dcs.log.close()


async def test_wait_falls_back_to_wall_clock_when_bus_offline():
    """plant-bus 未連線時 DCS 仍須能獨立運轉。"""
    dcs = _make_dcs()
    try:
        dcs.bus.connected.clear()
        result = await asyncio.wait_for(dcs._wait_sim_period(0.05, 0.0), timeout=1.0)
        assert result == dcs.sim_time
    finally:
        dcs.log.close()


async def test_wait_recovers_if_ticks_stop_while_connected():
    """安全網：已連線但模擬時間停滯時不得永久卡住控制迴圈。"""
    dcs = _make_dcs()
    try:
        dcs.bus.connected.set()
        dcs.SIM_TIME_STALL_TIMEOUT = 0.2
        dcs.sim_time = 5.0
        result = await asyncio.wait_for(dcs._wait_sim_period(1.0, 5.0), timeout=2.0)
        assert result == pytest.approx(5.0)
    finally:
        dcs.log.close()


async def test_controller_receives_ticks_without_observer():
    """DCS 的時間基準不能依賴 historian／HMI 是否在線。"""
    log = EventLogger(device="plant-bus")
    bus = PlantBus({"simulation": {"dt": 0.1}}, None, log)
    controller = _FakeWriter()
    bus.participants["dcs-plc"] = Participant(name="dcs-plc", role=Role.CONTROLLER.value,
                                              writer=controller)
    await bus._do_tick()
    assert MsgType.TICK.value in controller.types(), "沒有 observer 時控制器仍須收到 tick"
    assert controller.sent[-1]["sim_time"] > 0


# ---------------------------------------------------------------------------
# P1-2 給水泵：鍋爐禁止給水後仍以最低轉速持續送水
# ---------------------------------------------------------------------------
def test_feedwater_pump_stops_when_boiler_forbids_feedwater():
    """症狀：DCS 把 MANUAL_OUTPUT 設為 0，但 pump_base 的 min_speed／揚程下限
    又把它抬回去，結果泵浦仍以 52% 轉速送 19 kg/s 進禁止進水的鍋爐。"""
    plant = MiniPlant()
    try:
        bring_to_steady(plant, load_mw=40.0, seconds=300.0)
        pump = plant.dev("feedwater_pump")
        assert pump.speed > pump.min_speed, "前置條件：泵浦原本正在送水"

        for _ in range(300):                      # 30 模擬秒
            plant.signals["boiler.feedwater_permitted"] = 0.0
            plant.step(1)

        assert pump.speed < 0.5, f"禁止給水後泵浦必須停轉，實際 {pump.speed:.2f}%"
        assert pump.flow < 0.5, f"禁止給水後流量必須歸零，實際 {pump.flow:.2f} kg/s"
    finally:
        plant.close()


def test_feedwater_pump_resumes_after_permission_restored():
    """禁止解除後必須恢復送水，修正不得讓泵浦永久停擺。"""
    plant = MiniPlant()
    try:
        bring_to_steady(plant, load_mw=40.0, seconds=300.0)
        pump = plant.dev("feedwater_pump")
        for _ in range(100):
            plant.signals["boiler.feedwater_permitted"] = 0.0
            plant.step(1)
        assert pump.speed < 0.5

        plant.write("feedwater_pump", "MANUAL_OUTPUT", 45.0)
        for _ in range(200):
            plant.signals["boiler.feedwater_permitted"] = 1.0
            plant.step(1)
        assert pump.speed > pump.min_speed, "允許給水後必須恢復運轉"
        assert pump.flow > 1.0
    finally:
        plant.close()


# ---------------------------------------------------------------------------
# P1-3 故障注入：目標打錯字會廣播到全廠
# ---------------------------------------------------------------------------
async def test_unknown_fault_target_is_rejected_not_broadcast():
    """症狀：`boielr` 這種 typo 走 _request 的 broadcast 分支，全部設備都套用故障。"""
    bus, writers = _bus_with_devices("boiler", "turbine", "generator")
    result = await bus.inject_fault("boielr", {"action": "set", "category": "actuator",
                                               "name": "pump_trip", "spec": True}, timeout=0.05)
    assert result["error"], "未知目標必須明確回報錯誤"
    assert result["acked"] == []
    assert "boiler" in result["known_targets"]
    for name, writer in writers.items():
        assert MsgType.FAULT.value not in writer.types(), f"{name} 不該收到故障注入"


async def test_known_fault_target_is_point_to_point():
    bus, writers = _bus_with_devices("boiler", "turbine")
    await bus.inject_fault("boiler", {"action": "set", "category": "actuator",
                                      "name": "x", "spec": True}, timeout=0.05)
    assert MsgType.FAULT.value in writers["boiler"].types()
    assert MsgType.FAULT.value not in writers["turbine"].types()


async def test_wildcard_fault_target_still_broadcasts():
    bus, writers = _bus_with_devices("boiler", "turbine")
    await bus.inject_fault("*", {"action": "set", "category": "actuator",
                                 "name": "x", "spec": True}, timeout=0.05)
    for writer in writers.values():
        assert MsgType.FAULT.value in writer.types()


# ---------------------------------------------------------------------------
# P1-4 HTTP：畸形 JSON 被吞成 {}
# ---------------------------------------------------------------------------
async def _post_body(payload: str) -> tuple[int, dict]:
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"body": await _body(request)})

    app.router.add_post("/x", handler)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/x", data=payload,
                                     headers={"Content-Type": "application/json"})
        return response.status, await response.json()


async def test_malformed_json_body_returns_400():
    """症狀：`{` 這種不完整的 JSON 被當成 `{}`，/snapshot/restore 會意外
    退回 last_snapshot 執行 rollback 並回 200。"""
    status, payload = await _post_body("{")
    assert status == 400, f"畸形 JSON 必須回 400，實際 {status} / {payload}"


async def test_non_object_json_body_returns_400():
    status, _ = await _post_body("[1, 2, 3]")
    assert status == 400


async def test_empty_body_still_allowed():
    """/sim/pause 這類端點沒有 body，必須維持可用。"""
    status, payload = await _post_body("")
    assert status == 200 and payload["body"] == {}


# ---------------------------------------------------------------------------
# P1-5 快照：損毀或不完整的快照仍可還原
# ---------------------------------------------------------------------------
def test_corrupted_snapshot_fails_verification(tmp_path):
    """症狀：restore 前不驗 checksum，被竄改的快照照樣套用到機組上。"""
    store = SnapshotStore(str(tmp_path))
    store.save("base", {"tick": 5, "sim_time": 0.5, "signals": {}}, {"boiler": {"x": 1}})
    path = tmp_path / "base.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["participants"]["boiler"]["x"] = 999
    path.write_text(json.dumps(document), encoding="utf-8")

    assert not store.verify("base")
    with pytest.raises(SnapshotIntegrityError):
        store.load("base", verify=True)


def test_truncated_snapshot_file_fails_verification(tmp_path):
    store = SnapshotStore(str(tmp_path))
    store.save("base", {"tick": 1, "sim_time": 0.1, "signals": {}}, {"boiler": {}})
    (tmp_path / "base.json").write_text('{"meta": {', encoding="utf-8")
    assert not store.verify("base")
    with pytest.raises(SnapshotIntegrityError):
        store.load("base", verify=True)


def test_intact_snapshot_verifies(tmp_path):
    store = SnapshotStore(str(tmp_path))
    meta = store.save("base", {"tick": 5, "sim_time": 0.5, "signals": {}}, {"boiler": {"x": 1}})
    assert meta["complete"] is True and meta["missing"] == []
    assert store.verify("base")
    assert store.load("base", verify=True)["participants"]["boiler"]["x"] == 1


def test_partial_snapshot_is_flagged_and_refused(tmp_path):
    """症狀：離線設備造成的 partial snapshot 仍成為 last_snapshot，
    之後還原會形成部分設備新狀態、部分設備舊狀態的混合機組。"""
    store = SnapshotStore(str(tmp_path))
    meta = store.save("partial", {"tick": 5, "sim_time": 0.5, "signals": {}},
                      {"boiler": {"x": 1}}, missing=["turbine"])
    assert meta["complete"] is False and meta["missing"] == ["turbine"]

    async def scenario() -> dict:
        bus = PlantBus({}, store, EventLogger(device="plant-bus"))
        # 還原必須拒絕；明確指定 allow_incomplete 才放行
        with pytest.raises(SnapshotIntegrityError):
            await bus.restore_snapshot("partial", {})
        return await bus.restore_snapshot("partial", {"allow_incomplete": True})

    assert asyncio.run(scenario())["name"] == "partial"


async def test_incomplete_snapshot_does_not_become_last_snapshot(tmp_path):
    store = SnapshotStore(str(tmp_path))
    log = EventLogger(device="plant-bus")
    bus = PlantBus({"simulation": {"devices": ["boiler", "turbine"]}}, store, log)
    bus.participants["boiler"] = Participant(name="boiler", role=Role.DEVICE.value,
                                             writer=_FakeWriter())
    bus.participants["turbine"] = Participant(name="turbine", role=Role.DEVICE.value,
                                              writer=_FakeWriter())
    # 兩台設備都不會回 SNAPSHOT_DATA -> 完全不完整
    meta = await bus.save_snapshot("half", timeout=0.05)
    assert meta["complete"] is False
    assert bus.last_snapshot is None, "不完整的快照不得成為預設還原目標"


# ---------------------------------------------------------------------------
# P1-6 pause; step N 後暫停狀態分裂
# ---------------------------------------------------------------------------
async def test_step_broadcasts_resume_and_pause():
    """症狀：step 沒有廣播 RESUME/PAUSE，設備最後自行 free-run，
    plant-bus 卻仍顯示 paused。"""
    bus, writers = _bus_with_devices("boiler")
    await bus.pause()
    assert writers["boiler"].types()[-1] == MsgType.PAUSE.value
    writers["boiler"].sent.clear()

    await bus.step(3)
    assert MsgType.RESUME.value in writers["boiler"].types(), "step 必須先廣播 RESUME"
    assert not bus.paused and bus.step_budget == 3


async def test_step_completion_pauses_devices_again():
    bus, writers = _bus_with_devices("boiler")
    await bus.pause()
    writers["boiler"].sent.clear()
    await bus.step(2)
    bus._tick_idle.set()
    # 模擬 tick loop 把預算用完
    bus.step_budget = 0
    await bus.pause(reason="step_complete")
    assert bus.paused
    assert MsgType.PAUSE.value in writers["boiler"].types(), "步進結束必須廣播 PAUSE"


# ---------------------------------------------------------------------------
# P1-7 強電網模式把主蒸汽閥命令降到 0
# ---------------------------------------------------------------------------
def test_load_control_takes_over_bumplessly_in_grid_mode():
    """症狀：load_control 從未 to_auto()，手動輸出預設 0，
    強電網模式下閥門命令被壓成 0。"""
    from controller.dcs.main import DCS

    os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="dcs-reg-")
    dcs = DCS(config_dir=CONFIG_DIR)
    try:
        assert not dcs.load_control.auto, "初始必須是手動（由控制邏輯負責切換）"
        # 模擬「已併聯 + 強電網」時的接手：以目前閥位無擾動切入 AUTO
        dcs.load_control.to_auto(62.0)
        dcs.turbine_speed.to_manual(62.0)
        dcs.load_control.setpoint = 60.0
        output = dcs.load_control.update(60.0, 0.5)
        assert output == pytest.approx(62.0, abs=1.0), (
            f"bumpless transfer 後輸出應接近原閥位，實際 {output}"
        )
        assert output > 0.0
    finally:
        dcs.log.close()


def test_dcs_control_step_gate_allows_load_control():
    """閘門條件必須同時接受 turbine_speed.auto 或 load_control.auto，
    否則切到負載控制後整段 8.4 邏輯會停止執行。"""
    import inspect

    from controller.dcs.main import DCS

    source = inspect.getsource(DCS._control_step)
    assert "self.turbine_speed.auto or self.load_control.auto" in source


# ---------------------------------------------------------------------------
# P1-8 通訊故障 clear 讓設備主迴圈退出
# ---------------------------------------------------------------------------
def test_clear_unknown_fault_category_raises_value_error_not_key_error():
    """症狀：clear(category='comm') 觸發 KeyError('comm')，例外冒泡到主迴圈。"""
    injector = FaultInjector(enabled=True)
    with pytest.raises(ValueError):
        injector.clear("comm")


def test_clearing_comm_faults_does_not_crash_device():
    device = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
    try:
        device.comm_faults.freeze = True
        device.comm_faults.response_delay_ms = 250.0
        device._handle_fault({"payload": {"action": "clear", "category": "comm"}})
        assert not device.comm_faults.freeze
        assert device.comm_faults.response_delay_ms == 0.0
    finally:
        device.store.close()
        device.log.close()


def test_clearing_single_sensor_fault_keeps_comm_faults():
    """症狀：清除任一 sensor/process 故障都會無條件 reset 所有通訊故障。"""
    device = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
    try:
        device.comm_faults.response_delay_ms = 120.0
        device.faults.set("sensor", "pressure", {"mode": "bias", "bias": 3.0})
        device._handle_fault({"payload": {"action": "clear", "category": "sensor",
                                          "name": "pressure"}})
        assert "pressure" not in device.faults.sensors
        assert device.comm_faults.response_delay_ms == 120.0, "通訊故障不該被一併清掉"
    finally:
        device.store.close()
        device.log.close()


def test_clear_all_still_clears_comm_faults():
    device = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
    try:
        device.comm_faults.response_delay_ms = 120.0
        device.faults.set("sensor", "pressure", {"mode": "bias", "bias": 3.0})
        device._handle_fault({"payload": {"action": "clear"}})
        assert not device.faults.sensors
        assert device.comm_faults.response_delay_ms == 0.0
    finally:
        device.store.close()
        device.log.close()


# ---------------------------------------------------------------------------
# P1-9 安全故障模型實際無效
# ---------------------------------------------------------------------------
def test_burner_flame_loss_trips_flame_failure():
    """症狀：故障把 burner_command 壓成 0，而火焰失效判斷又看 burner_command，
    導致 FLAME_FAILURE 永遠不會觸發。"""
    plant = MiniPlant()
    try:
        bring_to_steady(plant, load_mw=40.0, seconds=300.0)
        boiler = plant.dev("boiler")
        assert boiler.flame == 2, "前置條件：火焰穩定"

        boiler.faults.set("actuator", "burner_flame_loss", True)
        plant.step(1)
        assert boiler.burner_demand > 0.0, "控制端仍在要求燃燒"
        assert boiler.flame == 0, "故障後火焰必須熄滅"
        assert boiler.flame_fail_flag == 1.0, "必須偵測到『要求燃燒但沒有火焰』"

        for _ in range(200):                      # 20 模擬秒，遠超過 1 秒跳機延遲
            plant.step(1)
        state = boiler.protection.states[5104]
        assert state.latched, "FLAME_FAILURE 必須鎖存跳機"
        assert boiler.sm.tripped
        # 跳機後不再要求燃燒，條件應自行消失（僅保留鎖存）
        assert boiler.flame_fail_flag == 0.0
    finally:
        plant.close()


def test_breaker_fail_to_open_keeps_breaker_closed():
    """症狀：breaker_fail_to_open 只點亮警報，斷路器照樣成功開路。"""
    plant = MiniPlant()
    try:
        bring_to_steady(plant, load_mw=40.0, seconds=500.0)
        generator = plant.dev("generator")
        if not generator.breaker_closed:
            pytest.skip("此次啟動未併聯，無法測試斷路器拒動")

        generator.faults.set("actuator", "breaker_fail_to_open", True)
        plant.pulse("generator", "BREAKER_OPEN")
        plant.step(5)
        assert generator.breaker_closed, "拒動故障下斷路器必須維持閉合"
        assert generator.alarms.states[5315].active, "必須發出斷路器拒動警報"

        generator.faults.clear("actuator", "breaker_fail_to_open")
        plant.pulse("generator", "BREAKER_OPEN")
        plant.step(5)
        assert not generator.breaker_closed, "故障解除後必須能正常開路"
    finally:
        plant.close()


def test_force_safe_opens_breaker():
    """症狀：FORCE_SAFE 線圈對發電機沒有任何作用。"""
    plant = MiniPlant()
    try:
        bring_to_steady(plant, load_mw=40.0, seconds=500.0)
        generator = plant.dev("generator")
        if not generator.breaker_closed:
            pytest.skip("此次啟動未併聯，無法測試 FORCE_SAFE")

        generator.coil[generator.rmap.offset_of(Table.COIL, "FORCE_SAFE")] = True
        plant.step(10)
        assert not generator.breaker_closed, "FORCE_SAFE 必須讓斷路器開路"
    finally:
        plant.close()


# ---------------------------------------------------------------------------
# P1-10 E-STOP 權限可透過 FC15 寫其他線圈
# ---------------------------------------------------------------------------
def _coil_server(applied: list) -> tuple[ModbusTcpServer, RegisterMap]:
    rmap = RegisterMap.build("boiler", Boiler.PROCESS_INPUTS, Boiler.EXTRA_HOLDINGS,
                             Boiler.EXTRA_COILS)
    access = AccessPolicy(write_allowlist=["10.0.0.9"], safety_allowlist=["10.9.9.9"],
                          enforce_single_writer=True)
    server = ModbusTcpServer(rmap, lambda: RegisterImage.empty(rmap),
                             lambda req: applied.append(req) or None, access=access)
    return server, rmap


def _fc15(low: int, count: int) -> bytes:
    byte_count = (count + 7) // 8
    payload = bytearray(byte_count)
    for i in range(count):
        payload[i // 8] |= 1 << (i % 8)
    return struct.pack(">HHB", low, count, byte_count) + bytes(payload)


def test_fc15_batch_including_estop_does_not_grant_safety_privilege():
    """症狀：只要批次範圍涵蓋 E-STOP 就整批視為 safety write，
    具安全權限的來源可同時寫 START、RESET_TRIP 等非安全線圈。"""
    applied: list = []
    server, rmap = _coil_server(applied)
    estop = rmap.offset_of(Table.COIL, "EMERGENCY_STOP")
    start = rmap.offset_of(Table.COIL, "START")
    low, high = min(estop, start), max(estop, start)

    response, _ = server._write_multiple_coils(_fc15(low, high - low + 1), "10.9.9.9")
    assert response[0] & 0x80, "混合批次必須被拒絕（回 Modbus 例外）"
    assert not applied, "任何線圈都不得被寫入"


def test_safety_source_can_still_write_estop_alone():
    """修正不得擋掉正常的安全來源 E-STOP 寫入。"""
    applied: list = []
    server, rmap = _coil_server(applied)
    estop = rmap.offset_of(Table.COIL, "EMERGENCY_STOP")

    response, _ = server._write_single_coil(struct.pack(">HH", estop, 0xFF00), "10.9.9.9")
    assert not response[0] & 0x80, "單獨寫 E-STOP 必須成功"
    assert applied and applied[0].offset == estop


def test_safety_source_can_write_contiguous_safety_coils():
    """E-STOP 與 FORCE_SAFE 相鄰，整批都是安全線圈時仍應放行。"""
    applied: list = []
    server, rmap = _coil_server(applied)
    estop = rmap.offset_of(Table.COIL, "EMERGENCY_STOP")
    force_safe = rmap.offset_of(Table.COIL, "FORCE_SAFE")
    assert abs(estop - force_safe) == 1

    low = min(estop, force_safe)
    response, _ = server._write_multiple_coils(_fc15(low, 2), "10.9.9.9")
    assert not response[0] & 0x80
    assert applied


def test_non_safety_source_still_blocked_on_estop():
    applied: list = []
    server, rmap = _coil_server(applied)
    estop = rmap.offset_of(Table.COIL, "EMERGENCY_STOP")
    response, _ = server._write_single_coil(struct.pack(">HH", estop, 0xFF00), "10.1.2.3")
    assert response[0] & 0x80
    assert not applied


# ---------------------------------------------------------------------------
# 其他：Trip matrix 動作失敗不重試
# ---------------------------------------------------------------------------
def test_trip_matrix_retries_failed_actions():
    """症狀：跳機當下 Modbus 寫失敗會永久漏掉連鎖保護。"""
    events: list[tuple] = []
    matrix = TripMatrix(emit=lambda name, **kw: events.append((name, kw)))

    first = matrix.evaluate({"turbine": True})
    assert first, "跳機邊緣必須產生連鎖動作"
    for action in first:
        matrix.confirm(action, ok=False)          # 全部寫入失敗

    retry = matrix.evaluate({"turbine": True})
    assert {a.key for a in retry} == {a.key for a in first}, "失敗的動作必須重試"

    for action in retry:
        matrix.confirm(action, ok=True)
    assert matrix.evaluate({"turbine": True}) == [], "成功後不得重複執行"
    assert any(name == "TRIP_MATRIX_ACTION_FAILED" for name, _ in events)


def test_trip_matrix_partial_failure_only_retries_failed_action():
    matrix = TripMatrix()
    actions = matrix.evaluate({"boiler": True})
    assert len(actions) == 2
    matrix.confirm(actions[0], ok=True)
    matrix.confirm(actions[1], ok=False)

    retry = matrix.evaluate({"boiler": True})
    assert [a.key for a in retry] == [actions[1].key]


def test_trip_matrix_clears_pending_when_trip_resets():
    matrix = TripMatrix()
    for action in matrix.evaluate({"boiler": True}):
        matrix.confirm(action, ok=False)
    assert matrix.pending_actions > 0
    matrix.evaluate({"boiler": False})
    assert matrix.pending_actions == 0


def test_trip_matrix_snapshot_round_trip_keeps_pending():
    matrix = TripMatrix()
    for action in matrix.evaluate({"boiler": True}):
        matrix.confirm(action, ok=False)
    data = matrix.to_dict()

    restored = TripMatrix()
    restored.from_dict(data)
    assert restored.pending_actions == matrix.pending_actions
    assert {a.key for a in restored.evaluate({"boiler": True})} == \
           {a.key for a in matrix.evaluate({"boiler": True})}


def test_trip_matrix_does_not_mutate_shared_default_rules():
    from controller import trip_matrix as module

    TripMatrix()
    assert all(a.source == "" for rule in module.DEFAULT_MATRIX for a in rule.actions)


# ---------------------------------------------------------------------------
# 其他：感測器故障同一 tick 被多次取樣
# ---------------------------------------------------------------------------
def test_sensor_drift_advances_once_per_scan():
    """症狀：drift 在 protection_values / publish / fill_registers 各取樣一次，
    漂移速率變成設定值的 3 倍。"""
    plant = MiniPlant()
    try:
        turbine = plant.dev("turbine")
        turbine.faults.set("sensor", "speed", {"mode": "drift", "drift": 1.0})
        plant.step(10)                            # 1 模擬秒
        drift = turbine.faults.sensors["speed"]._drift_acc
        assert drift == pytest.approx(1.0, abs=0.05), f"1 秒應漂移 1.0，實際 {drift}"
    finally:
        plant.close()


def test_noisy_sensor_is_consistent_across_consumers():
    """保護邏輯、Modbus 映像與 SimBus 發佈值必須看到同一個讀值。"""
    plant = MiniPlant()
    try:
        turbine = plant.dev("turbine")
        turbine.faults.set("sensor", "speed", {"mode": "noise", "noise": 20.0})
        plant.step(1)
        protection = turbine.protection_values()["speed_rpm"]
        published = turbine.publish()["turbine.speed_rpm"]
        regs = [0] * turbine.rmap.input_size
        turbine.fill_registers(regs)
        assert protection == pytest.approx(published), "保護值與發佈值必須一致"
        assert regs[9] == pytest.approx(round(max(0.0, published)), abs=1)
    finally:
        plant.close()


def test_sensor_cache_refreshes_between_scans():
    """快取只在單一 scan 內有效，不得凍結讀值。"""
    plant = MiniPlant()
    try:
        turbine = plant.dev("turbine")
        turbine.faults.set("sensor", "speed", {"mode": "drift", "drift": 10.0})
        plant.step(1)
        first = turbine.publish()["turbine.speed_rpm"]
        plant.step(1)
        second = turbine.publish()["turbine.speed_rpm"]
        assert first != second, "跨 scan 必須重新取樣"
    finally:
        plant.close()


# ---------------------------------------------------------------------------
# 其他：LAB_MODE=false 時快照可重新啟用故障注入
# ---------------------------------------------------------------------------
def test_snapshot_cannot_reenable_fault_injection_outside_lab_mode():
    """症狀：快照帶著 enabled=true 的故障進到 LAB_MODE=false 的機組，
    而且之後無法由 API 清除。"""
    previous = os.environ.get("LAB_MODE")
    try:
        os.environ["LAB_MODE"] = "true"
        lab = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
        lab.faults.set("actuator", "burner_flame_loss", True)
        lab.comm_faults.freeze = True
        state = lab.snapshot_state()
        lab.store.close()
        lab.log.close()

        os.environ["LAB_MODE"] = "false"
        prod = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
        try:
            assert not prod.faults.enabled
            prod.restore_state(state, {})
            assert not prod.faults.enabled, "LAB_MODE=false 不得由快照重新啟用故障注入"
            assert not prod.faults.actuator("burner_flame_loss")
            assert not prod.comm_faults.freeze
        finally:
            prod.store.close()
            prod.log.close()
    finally:
        if previous is None:
            os.environ.pop("LAB_MODE", None)
        else:
            os.environ["LAB_MODE"] = previous


def test_snapshot_keeps_faults_in_lab_mode():
    """LAB_MODE=true 時快照仍必須完整還原故障設定。"""
    previous = os.environ.get("LAB_MODE")
    try:
        os.environ["LAB_MODE"] = "true"
        source = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
        source.faults.set("actuator", "burner_flame_loss", True)
        source.comm_faults.response_delay_ms = 75.0
        state = source.snapshot_state()
        source.store.close()
        source.log.close()

        target = Boiler(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
        try:
            target.restore_state(state, {})
            assert target.faults.enabled
            assert target.faults.actuator("burner_flame_loss")
            assert target.comm_faults.response_delay_ms == 75.0
        finally:
            target.store.close()
            target.log.close()
    finally:
        if previous is None:
            os.environ.pop("LAB_MODE", None)
        else:
            os.environ["LAB_MODE"] = previous


# ---------------------------------------------------------------------------
# 其他：發電機過頻／欠頻共用 alarm code
# ---------------------------------------------------------------------------
def test_over_and_under_frequency_have_distinct_alarm_codes():
    """症狀：兩者共用 5312，後評估的欠頻會把過頻狀態覆寫掉。"""
    codes = {d["name"]: d["alarm_code"] for d in Generator.PROTECTION_DEFS}
    assert codes["OVERFREQUENCY"] != codes["UNDERFREQUENCY"]
    alarm_codes = {spec.name: spec.code for spec in Generator.ALARMS}
    assert alarm_codes["OVERFREQUENCY"] == codes["OVERFREQUENCY"]
    assert alarm_codes["UNDERFREQUENCY"] == codes["UNDERFREQUENCY"]
    bits = [spec.bit for spec in Generator.ALARMS]
    assert len(bits) == len(set(bits)), "警報位元不得重複"


def test_overfrequency_alarm_survives_underfrequency_evaluation():
    """兩個保護都會在同一次 evaluate 呼叫 alarm hook；
    共用 code 時後評估的欠頻會把剛成立的過頻警報清掉。"""
    device = Generator(config_dir=CONFIG_DIR, state_dir=tempfile.mkdtemp())
    try:
        device.speed_rpm = 3090.0        # 解除 OVERFREQUENCY 的低轉速抑制
        device.breaker_closed = 1        # 解除 UNDERFREQUENCY 的未併聯抑制
        values = {"frequency": 51.5, "current_pu": 0.0, "reverse_power": 0.0}
        device.protection.evaluate(0.1, values, 1.0)

        assert device.alarms.states[5312].active, "過頻警報必須成立且不被欠頻覆寫"
        assert not device.alarms.states[5316].active, "欠頻不得同時成立"

        device.protection.evaluate(0.1, {**values, "frequency": 48.0}, 1.1)
        assert not device.alarms.states[5312].active
        assert device.alarms.states[5316].active, "欠頻警報必須獨立成立"
    finally:
        device.store.close()
        device.log.close()


# ---------------------------------------------------------------------------
# 其他：historian 還原到較早快照後停止取樣
# ---------------------------------------------------------------------------
def test_historian_resumes_sampling_after_timeline_rewind(tmp_path):
    """症狀：還原到較早的快照後，historian 會停止取樣直到追過舊時間線。"""
    os.environ["STATE_DIR"] = str(tmp_path)
    from historian.main import Historian

    historian = Historian()
    try:
        for sim_time in (0.0, 1.0, 2.0, 3.0):
            historian._on_tick({"sim_time": sim_time, "tick": int(sim_time * 10),
                                "inputs": {"boiler.pressure_bar_abs": {"value": sim_time}}})
        before = historian.sample_count
        assert before >= 3

        historian._on_tick({"sim_time": 0.5, "tick": 5,
                            "inputs": {"boiler.pressure_bar_abs": {"value": 9.0}}})
        assert historian.sample_count > before, "時間軸回捲後必須立即恢復取樣"
    finally:
        historian.conn.close()
        historian.log.close()
        os.environ.pop("STATE_DIR", None)


# ---------------------------------------------------------------------------
# 其他：scenario runner 忽略 API 錯誤與使用舊事件
# ---------------------------------------------------------------------------
def test_scenario_runner_reports_api_errors(tmp_path):
    """症狀：/fault 等呼叫回傳 error 時仍被視為成功。"""
    from tools.scenario_runner import run_scenario

    scenario = tmp_path / "s.yaml"
    scenario.write_text(
        "name: t\nsteps:\n  - fault:\n      target: boielr\n      name: x\n",
        encoding="utf-8",
    )

    def api(path, method="GET", body=None):
        if path == "/fault":
            return {"target": "boielr", "acked": [], "error": "未知的故障目標 boielr"}
        return {"signals": {}, "sim_time": 0.0, "wall_time": ""}

    exit_code = run_scenario(str(scenario), api, lambda *a, **k: {"ok": True}, verbose=False)
    assert exit_code == 1, "API 錯誤必須讓情境 FAIL"


def test_scenario_runner_ignores_events_from_before_the_step(tmp_path):
    """症狀：事件 ring 裡的舊事件會讓情境誤判 PASS。"""
    from tools.scenario_runner import _expect_event

    old_event = {"event": "TRIP_LATCHED", "device": "boiler",
                 "wall_time": "2020-01-01T00:00:00.000+00:00", "sim_time": 1.0}

    def api(path, method="GET", body=None):
        if path == "/state":
            return {"wall_time": "2026-01-01T00:00:00.000+00:00", "sim_time": 500.0,
                    "signals": {}}
        return [old_event]

    ok, detail = _expect_event(api, {"event": "TRIP_LATCHED", "device": "boiler", "within": 0.2})
    assert not ok, f"不得採用步驟開始前的舊事件（{detail}）"
