"""规则层测试，对应实施方案 10.1 节 R1–R11。时间全部由测试控制。"""
from common.enums import AlarmType, NavState, Quality, Risk, Verdict
from edge.rules.alarms import EvalContext, Observation, SpeedZoneLite, Telemetry, evaluate
from edge.rules.engine import DurationRule, OutputKind, RuleParamsLite

A1 = AlarmType.A1_BRIDGE_ABSENCE
ZONE = SpeedZoneLite("测试区", 8.0, [[0, 0], [10, 0], [10, 10], [0, 10]])


def rule(trigger=10, recover=3, repeat=30, alarm=A1):
    return DurationRule(alarm, RuleParamsLite(trigger, recover, repeat, Risk.HIGH))


def ctx(now, count=0, quality=Quality.VALID, nav=NavState.UNDERWAY, tel_age=0.0, speed=6.0, pos=(5, 5)):
    return EvalContext(
        now=now,
        obs=Observation(ts=now, quality=quality, roi_count=count),
        telemetry=Telemetry(sampled_at=now - tel_age, nav_state=nav, speed_kn=speed, position=list(pos)),
        last_ais_at=now,
        speed_zones=[ZONE],
    )


def run(r, alarm, timeline, step=0.1, extra=None):
    """timeline: [(开始秒, ctx 参数字典), ...]，按 step 推进，返回所有输出。"""
    outs = []
    end = timeline[-1][0]
    t = 0.0
    while t <= end + 1e-9:
        kwargs = [kw for start, kw in timeline if start <= t + 1e-9][-1]
        v, d = evaluate(alarm, ctx(t, **kwargs), extra or {})
        outs += r.update(round(t, 3), v, d)
        t = round(t + step, 3)
    return outs


def kinds(outs):
    return [o.kind for o in outs]


def test_r1_below_threshold_no_alarm():
    outs = run(rule(), A1, [(0, {"count": 1}), (1, {"count": 0}), (10.9, {"count": 1})])
    assert outs == []


def test_r2_threshold_triggers_once():
    r = rule()
    outs = run(r, A1, [(0, {"count": 0}), (10.0, {"count": 0})])
    assert kinds(outs) == [OutputKind.CREATED]
    assert outs[0].condition_satisfied_at == 10.0
    assert outs[0].started_at == 0.0


def test_r3_long_absence_same_event_with_reminders():
    outs = run(rule(), A1, [(0, {"count": 0}), (70, {"count": 0})])
    assert kinds(outs) == [OutputKind.CREATED, OutputKind.REMINDED, OutputKind.REMINDED]
    assert len({o.event_id for o in outs}) == 1


def test_r4_return_during_pending_resets():
    outs = run(rule(), A1, [(0, {"count": 0}), (6, {"count": 1}), (7, {"count": 0}), (16, {"count": 0})])
    assert outs == []  # 第二段无人只有 9 秒


def test_r5_short_return_does_not_recover():
    outs = run(rule(), A1, [(0, {"count": 0}), (12, {"count": 1}), (14, {"count": 0}), (20, {"count": 0})])
    assert kinds(outs) == [OutputKind.CREATED]


def test_r6_recover_after_sustained_presence():
    outs = run(rule(), A1, [(0, {"count": 0}), (12, {"count": 1}), (16, {"count": 1})])
    assert kinds(outs) == [OutputKind.CREATED, OutputKind.RECOVERED]
    assert outs[1].at == 15.0
    assert outs[0].event_id == outs[1].event_id


def test_r7_black_screen_resets_absence_and_starts_camera_rule():
    a1 = rule()
    a3 = rule(trigger=5, alarm=AlarmType.A3_CAMERA_ABNORMAL)
    tl = [(0, {"count": 0}), (6, {"quality": Quality.DARK}), (12, {"quality": Quality.DARK})]
    assert run(a1, A1, tl) == []
    outs = run(a3, AlarmType.A3_CAMERA_ABNORMAL, tl)
    assert kinds(outs) == [OutputKind.CREATED]
    assert outs[0].detail["subtype"] == "BLACK_SCREEN"
    assert outs[0].condition_satisfied_at == 11.0


def test_r7b_unknown_after_trigger_marks_event_not_recovered():
    outs = run(rule(), A1, [(0, {"count": 0}), (11, {"quality": Quality.NO_FRAME}),
                            (13, {"count": 0}), (14, {"count": 0})])
    assert kinds(outs) == [OutputKind.CREATED, OutputKind.UNKNOWN, OutputKind.OBSERVABLE]


def test_r8_moored_or_stale_telemetry_does_not_trigger():
    assert run(rule(), A1, [(0, {"count": 0, "nav": NavState.MOORED}), (30, {"count": 0, "nav": NavState.MOORED})]) == []
    assert run(rule(), A1, [(0, {"count": 0, "tel_age": 5}), (30, {"count": 0, "tel_age": 5})]) == []


def test_r8b_moored_after_trigger_ends_by_precondition():
    outs = run(rule(), A1, [(0, {"count": 0}), (12, {"count": 0, "nav": NavState.MOORED})])
    assert kinds(outs) == [OutputKind.CREATED, OutputKind.PRECONDITION_ENDED]


A6 = AlarmType.A6_OVERSPEED
MARGINS = {"enter_margin_kn": 0.5, "exit_margin_kn": 0.2}


def test_r9_overspeed_trigger_and_hysteresis():
    r = rule(trigger=5, recover=2, repeat=1000, alarm=A6)
    tl = [(0, {"speed": 8.6}), (6, {"speed": 8.3}), (7, {"speed": 8.6}), (8, {"speed": 8.3}), (20, {"speed": 8.3})]
    outs = run(r, A6, tl, extra=MARGINS)
    assert kinds(outs) == [OutputKind.CREATED]  # 8.3 节处于防抖区间，不恢复也不重触发


def test_r9b_hold_resets_pending():
    tl = [(0, {"speed": 8.6}), (4, {"speed": 8.3}), (5, {"speed": 8.6}), (9.5, {"speed": 8.6})]
    assert run(rule(trigger=5, alarm=A6), A6, tl, extra=MARGINS) == []


def test_r9c_overspeed_outside_zone_is_normal():
    tl = [(0, {"speed": 12, "pos": (20, 20)}), (20, {"speed": 12, "pos": (20, 20)})]
    assert run(rule(trigger=5, alarm=A6), A6, tl, extra=MARGINS) == []


def test_r10_stale_telemetry_no_overspeed():
    tl = [(0, {"speed": 12, "tel_age": 5}), (20, {"speed": 12, "tel_age": 5})]
    assert run(rule(trigger=5, alarm=A6), A6, tl, extra=MARGINS) == []


def test_r11_stream_lost_triggers_camera_and_absence_unknown():
    a3 = rule(trigger=5, alarm=AlarmType.A3_CAMERA_ABNORMAL)
    outs = run(a3, AlarmType.A3_CAMERA_ABNORMAL, [(0, {"quality": Quality.NO_FRAME}), (6, {"quality": Quality.NO_FRAME})])
    assert kinds(outs) == [OutputKind.CREATED]
    assert outs[0].detail["subtype"] == "STREAM_LOST"
    v, _ = evaluate(A1, ctx(1, quality=Quality.NO_FRAME), {})
    assert v == Verdict.UNKNOWN


def test_r11b_ended_or_warmup_is_not_camera_fault():
    for q in (Quality.ENDED, Quality.WARMUP, Quality.STALE):
        v, _ = evaluate(AlarmType.A3_CAMERA_ABNORMAL, ctx(1, quality=q), {})
        assert v != Verdict.ABNORMAL


def test_ais_offline():
    r = rule(trigger=15, recover=0, alarm=AlarmType.A5_AIS_OFFLINE)
    outs = []
    for i in range(0, 300):
        t = i / 10
        last = t if t < 5 or t >= 25 else 5.0  # 5~25 秒无 AIS 报文
        c = EvalContext(now=t, obs=None, telemetry=None, last_ais_at=last)
        v, d = evaluate(AlarmType.A5_AIS_OFFLINE, c, {})
        outs += r.update(t, v, d)
    assert kinds(outs) == [OutputKind.CREATED, OutputKind.RECOVERED]


def test_interrupt_emits_only_when_active():
    r = rule()
    assert r.interrupt(1, "x") == []
    run(r, A1, [(0, {"count": 0}), (11, {"count": 0})])
    outs = r.interrupt(12, "视频结束")
    assert kinds(outs) == [OutputKind.INTERRUPTED]
    assert not r.active
