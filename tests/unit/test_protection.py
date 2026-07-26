"""跳機鎖存、遲滯、第一故障原因。"""
from common.device.protection import ProtectionEngine, ProtectionSpec

SPECS = [
    ProtectionSpec(code=1, name="LOW_LOW_LEVEL", signal="level", direction="low",
                   alarm_threshold=30.0, trip_threshold=20.0, delay=2.0,
                   reset_threshold=30.0, reset_delay=5.0, alarm_code=11),
    ProtectionSpec(code=2, name="HIGH_PRESSURE", signal="pressure", direction="high",
                   alarm_threshold=108.0, trip_threshold=115.0, delay=1.0,
                   reset_threshold=105.0, reset_delay=5.0, alarm_code=12),
]


def make_engine():
    events = []
    engine = ProtectionEngine("boiler", [ProtectionSpec(**s.__dict__) for s in SPECS],
                              emit=lambda e, **kw: events.append((e, kw)))
    return engine, events


def test_trip_requires_delay():
    engine, _ = make_engine()
    for _ in range(19):
        engine.evaluate(0.1, {"level": 18.0, "pressure": 100.0}, 0.0)
    assert not engine.any_latched
    engine.evaluate(0.1, {"level": 18.0, "pressure": 100.0}, 0.0)
    assert engine.any_latched and engine.first_out_code() == 1


def test_latch_survives_condition_clearing_and_reset_needs_hysteresis():
    engine, _ = make_engine()
    for _ in range(25):
        engine.evaluate(0.1, {"level": 18.0, "pressure": 100.0}, 0.0)
    assert engine.states[1].active and engine.states[1].latched
    # 數值恢復但未達重置門檻
    for _ in range(60):
        engine.evaluate(0.1, {"level": 25.0, "pressure": 100.0}, 0.0)
    assert not engine.states[1].active
    assert engine.states[1].latched
    assert not engine.reset()
    # 超過重置門檻並持續 reset_delay
    for _ in range(60):
        engine.evaluate(0.1, {"level": 35.0, "pressure": 100.0}, 1.0)
    assert engine.states[1].resettable
    assert engine.reset()
    assert not engine.any_latched and engine.first_out is None


def test_first_out_not_overwritten_by_later_trips():
    engine, _ = make_engine()
    for _ in range(25):
        engine.evaluate(0.1, {"level": 18.0, "pressure": 100.0}, 1.0)
    for _ in range(25):
        engine.evaluate(0.1, {"level": 18.0, "pressure": 130.0}, 2.0)
    assert engine.first_out.code == 1
    assert engine.first_out.name == "LOW_LOW_LEVEL"
    assert engine.states[2].latched and not engine.states[2].first_out


def test_first_out_records_trend_and_context():
    engine, _ = make_engine()
    for step in range(120):
        engine.evaluate(0.1, {"level": 40.0 - step * 0.4, "pressure": 100.0},
                        step * 0.1, control_output=55.0)
    assert engine.first_out is not None
    assert engine.first_out.control_output == 55.0
    assert engine.first_out.pre_trend, "應保留跳機前的趨勢"
    assert engine.first_out.threshold == 20.0


def test_trip_word_and_persistence_round_trip():
    engine, _ = make_engine()
    for _ in range(25):
        engine.evaluate(0.1, {"level": 18.0, "pressure": 100.0}, 0.0)
    payload = engine.to_dict()
    restored, _ = make_engine()
    restored.from_dict(payload)
    assert restored.any_latched
    assert restored.trip_word() == engine.trip_word() != 0
    assert restored.first_out.code == 1
