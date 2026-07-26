"""PID：anti-windup、bumpless transfer、速率限制、死區。"""
from controller.pid import PID, ThreeElementLevel


def test_anti_windup_stops_integral_growth_at_saturation():
    pid = PID("t", kp=1.0, ki=1.0, setpoint=100.0, out_min=0.0, out_max=10.0,
              rate_up=1000, rate_down=1000, integral_limit=1000)
    pid.to_auto(0.0)
    for _ in range(200):
        pid.update(0.0, 0.1)
    assert pid.output == 10.0
    assert pid.integral <= 10.5, "飽和時積分項不應持續累積"


def test_bumpless_transfer():
    pid = PID("t", kp=2.0, ki=0.05, setpoint=50.0, out_min=0.0, out_max=100.0)
    pid.to_manual(42.0)
    pid.update(10.0, 0.5)
    assert abs(pid.output - 42.0) < 1e-9
    pid.to_auto()
    first = pid.update(10.0, 0.5)
    assert abs(first - 42.0) < 5.0, "切自動時輸出不可跳變"


def test_rate_limit():
    pid = PID("t", kp=10.0, ki=0.0, setpoint=100.0, out_min=0, out_max=100,
              rate_up=5.0, rate_down=10.0)
    pid.to_auto(0.0)
    pid.update(0.0, 1.0)
    assert abs(pid.output - 5.0) < 1e-6


def test_deadband():
    pid = PID("t", kp=1.0, ki=1.0, setpoint=100.0, deadband=5.0, out_min=-100, out_max=100)
    pid.to_auto(0.0)
    for _ in range(20):
        pid.update(97.0, 0.5)
    assert abs(pid.output) < 1e-6


def test_force_output_overrides_and_syncs_integral():
    pid = PID("t", kp=1.0, ki=1.0, setpoint=100.0, out_min=0, out_max=100)
    pid.to_auto(50.0)
    for _ in range(10):
        pid.update(0.0, 0.5)
    pid.force_output(0.0)
    assert pid.output == 0.0 and pid.integral == 0.0
    assert pid.update(0.0, 0.5) <= 55.0, "強制輸出後不應立刻彈回原積分值"


def test_three_element_uses_steam_feedforward():
    level = PID("level", kp=1.0, ki=0.0, setpoint=66.7, out_min=-50, out_max=50)
    flow = PID("flow", kp=1.0, ki=0.0, out_min=0, out_max=100)
    level.to_auto(0.0)
    flow.to_auto(0.0)
    three = ThreeElementLevel(level, flow, feedforward_gain=1.0)
    out_low = three.update(66.7, 20.0, 20.0, 0.5, rated_flow=120.0)
    out_high = three.update(66.7, 100.0, 20.0, 0.5, rated_flow=120.0)
    assert out_high > out_low, "蒸汽流量增加時前饋應提高給水命令"
