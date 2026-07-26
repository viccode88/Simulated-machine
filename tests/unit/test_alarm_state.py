from common.device.alarm import AlarmManager, AlarmSpec
from common.device.state_machine import DEFAULT_TRANSITIONS, StateMachine
from common.modbus.register_map import DeviceState

SPECS = [AlarmSpec(1, "A", 0, "第一個"), AlarmSpec(2, "B", 16, "第二個")]


def test_alarm_words_and_ack():
    events = []
    manager = AlarmManager(SPECS, emit=lambda e, **kw: events.append(e))
    manager.set(1, True, 5.0, 4.0)
    manager.set(2, True)
    assert manager.words() == (0b1, 0b1)
    assert manager.any_active and manager.any_unacked
    manager.set(1, False)
    assert manager.words()[0] == 0b1, "解除後仍鎖存直到確認"
    manager.ack_all()
    assert manager.words()[0] == 0
    assert "ALARM_SET" in events and "ALARM_CLEARED" in events


def test_alarm_round_trip():
    manager = AlarmManager(SPECS)
    manager.set(1, True)
    other = AlarmManager(SPECS)
    other.from_dict(manager.to_dict())
    assert other.words() == manager.words()


def test_state_machine_rejects_illegal_transition():
    rejected = []
    sm = StateMachine(DeviceState.TRIPPED, DEFAULT_TRANSITIONS,
                      on_reject=lambda a, b, r: rejected.append((a, b)))
    assert not sm.to(DeviceState.RUNNING)
    assert rejected
    assert sm.to(DeviceState.OFF)


def test_force_always_allows_trip():
    sm = StateMachine(DeviceState.RUNNING)
    sm.force(DeviceState.TRIPPED, "protection")
    assert sm.tripped
