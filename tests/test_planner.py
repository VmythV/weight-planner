import math
import random
from datetime import date, datetime, timedelta

import pytest

from app import planner
from app.planner import Profile, Weighin

START = date(2026, 1, 1)


def make_profile(**overrides) -> Profile:
    base = dict(
        sex="male",
        age=30,
        height_cm=175,
        activity="light",
        target_weight=75.0,
        plan_start=START,
        target_date=START + timedelta(days=90),
    )
    base.update(overrides)
    return Profile(**base)


def daily_weighins(first_day: date, weights: list[float | None], hour: int = 7) -> list[Weighin]:
    return [
        Weighin(datetime.combine(first_day + timedelta(days=i), datetime.min.time()).replace(hour=hour), w)
        for i, w in enumerate(weights)
        if w is not None
    ]


# ---------- 能量模型与计划曲线 ----------


def test_bmr_and_tdee_match_mifflin_st_jeor():
    p = make_profile()
    assert planner.bmr(85, p) == pytest.approx(1798.75)
    assert planner.tdee(85, p) == pytest.approx(1798.75 * 1.375)
    female = make_profile(sex="female")
    assert planner.bmr(85, female) == pytest.approx(1798.75 - 166)


def test_solve_intake_matches_worked_example_and_hits_target():
    p = make_profile()
    intake = planner.solve_intake(85, 75, 90, p)
    assert intake == pytest.approx(1547, abs=2)
    curve = planner.curve_for_intake(85, START, intake, p)
    assert curve.weight_at(START + timedelta(days=90)) == pytest.approx(75, abs=1e-9)


def test_closed_form_agrees_with_day_by_day_simulation():
    p = make_profile()
    intake = planner.solve_intake(85, 75, 90, p)
    weight = 85.0
    for _ in range(90):
        weight -= (planner.tdee(weight, p) + p.exercise_kcal - intake) / planner.KCAL_PER_KG
    assert weight == pytest.approx(75, abs=0.05)


def test_curve_is_front_loaded():
    p = make_profile()
    curve = planner.curve_for_intake(85, START, planner.solve_intake(85, 75, 90, p), p)
    first_month = 85 - curve.weight_at(START + timedelta(days=30))
    last_month = curve.weight_at(START + timedelta(days=60)) - 75
    assert first_month > last_month


def test_exercise_raises_allowed_intake_one_for_one():
    base = planner.solve_intake(85, 75, 90, make_profile())
    with_exercise = planner.solve_intake(85, 75, 90, make_profile(exercise_kcal=300))
    assert with_exercise - base == pytest.approx(300)


def test_days_to_reach():
    p = make_profile()
    intake = planner.solve_intake(85, 75, 90, p)
    curve = planner.curve_for_intake(85, START, intake, p)
    assert curve.days_to_reach(75) == pytest.approx(90)
    assert curve.days_to_reach(85) == 0
    assert curve.days_to_reach(curve.w_eq - 1) is None


# ---------- 安全检查 ----------


def test_worked_example_plan_is_safe():
    check = planner.check_plan(85, make_profile())
    assert not check.below_floor
    assert not check.too_fast
    assert check.suggested_date is None
    assert check.weekly_loss_pct == pytest.approx(0.99, abs=0.01)


def test_aggressive_plan_is_flagged_with_earliest_safe_date():
    p = make_profile(target_date=START + timedelta(days=45))
    check = planner.check_plan(85, p)
    assert check.too_fast and check.below_floor
    assert check.extra_exercise_needed > 0
    days = (check.suggested_date - START).days
    assert planner._is_safe(85, days, p)
    assert not planner._is_safe(85, days - 1, p)


def test_no_safe_date_when_maintenance_is_below_floor():
    p = make_profile(sex="female", age=60, height_cm=150, activity="sedentary", target_weight=40)
    assert planner.tdee(40, p) < p.intake_floor
    assert planner.earliest_safe_date(60, p) is None


# ---------- 趋势体重 ----------


def test_daily_weights_keeps_earliest_reading_of_each_day():
    day = datetime(2026, 1, 1)
    readings = [
        Weighin(day.replace(hour=21), 81.0),
        Weighin(day.replace(hour=7), 80.0),
        Weighin(day.replace(hour=12), 80.5),
    ]
    assert planner.daily_weights(readings) == [(date(2026, 1, 1), 80.0)]


def test_trend_has_no_lag_on_linear_decline():
    daily = [(START + timedelta(days=i), 85 - 0.12 * i) for i in range(40)]
    for (day, weight), (_, trend) in list(zip(daily, planner.trend_series(daily)))[2:]:
        assert trend == pytest.approx(weight, abs=1e-9)


def test_trend_handles_gaps_and_few_points():
    daily = [(START, 80.0), (START + timedelta(days=3), 79.0)]
    trend = planner.trend_series(daily)
    assert trend[0] == (START, 80.0)
    assert 79.0 < trend[1][1] < 80.0  # 两个点只做加权平均


def test_trend_smooths_daily_noise():
    rnd = random.Random(0)
    daily = [(START + timedelta(days=i), 80 + rnd.gauss(0, 0.45)) for i in range(60)]
    trend = planner.trend_series(daily)
    raw_rmse = math.sqrt(sum((w - 80) ** 2 for _, w in daily[14:]) / 46)
    trend_rmse = math.sqrt(sum((t - 80) ** 2 for _, t in trend[14:]) / 46)
    assert trend_rmse < raw_rmse * 0.6


# ---------- 仪表盘 ----------


def test_dashboard_requires_profile_and_data():
    assert planner.build_dashboard(None, [], START) == {"ready": False, "reason": "no_profile"}
    assert planner.build_dashboard(make_profile(), [], START) == {"ready": False, "reason": "no_data"}
    with_start = planner.build_dashboard(make_profile(start_weight=85), [], START)
    assert with_start["ready"] and with_start["card"]["status"] == "not_started"


def test_dashboard_on_track_when_following_plan():
    p = make_profile()
    intake = planner.solve_intake(85, 75, 90, p)
    curve = planner.curve_for_intake(85, START, intake, p)
    weights = [curve.weight_at(START + timedelta(days=i)) for i in range(30)]
    today = START + timedelta(days=29)
    dash = planner.build_dashboard(p, daily_weighins(START, weights), today)
    card = dash["card"]
    assert card["status"] == "on_track"
    assert abs(card["diff"]) < 0.05
    assert card["intake"] == pytest.approx(intake, abs=1)
    assert card["deficit"] == card["burn_total"] - card["intake"]
    assert not [w for w in dash["warnings"] if w["level"] in ("warn", "danger")]
    lengths = {len(v) for v in dash["series"].values()}
    assert lengths == {len(dash["series"]["dates"])}


def test_dashboard_behind_recalibrates_and_detects_plateau():
    p = make_profile()
    today = START + timedelta(days=21)
    dash = planner.build_dashboard(p, daily_weighins(START, [85.0] * 22), today)
    assert dash["card"]["status"] == "behind"
    checkins = dash["checkins"]
    assert [c["day"] for c in checkins] == [(START + timedelta(days=7 * k)).isoformat() for k in range(4)]
    assert checkins[-1]["required_intake"] < checkins[0]["required_intake"]
    assert checkins[-1]["recommended_intake"] >= p.intake_floor
    assert any("平台期" in w["text"] for w in dash["warnings"])


def test_dashboard_reached_switches_to_maintenance():
    p = make_profile()
    weights = [85 - 0.5 * i for i in range(25)]  # 第 24 天降到 73
    today = START + timedelta(days=24)
    dash = planner.build_dashboard(p, daily_weighins(START, weights), today)
    card = dash["card"]
    assert card["status"] == "reached" and card["reached"]
    assert card["intake"] == pytest.approx(planner.tdee(card["trend_weight"], p), abs=1)
    assert any(w["level"] == "success" for w in dash["warnings"])
    assert all(v is None for v in dash["series"]["projection"])


def test_dashboard_flags_unsafe_plan_and_stale_data():
    p = make_profile(target_date=START + timedelta(days=45))
    dash = planner.build_dashboard(p, daily_weighins(START, [85.0]), START + timedelta(days=5))
    levels = [w["level"] for w in dash["warnings"]]
    assert levels.count("danger") == 2
    assert dash["plan"]["suggested_date"] is not None
    assert any("没有称重" in w["text"] for w in dash["warnings"])


def test_slightly_fast_plan_is_a_warning_not_danger():
    p = make_profile(target_date=START + timedelta(days=80), exercise_kcal=200)
    dash = planner.build_dashboard(p, daily_weighins(START, [85.0]), START)
    assert 1.0 < dash["plan"]["weekly_loss_pct"] < 1.5
    assert [w["level"] for w in dash["warnings"]] == ["warn"]
    assert "偏快" in dash["warnings"][0]["text"]


def test_dashboard_after_target_date():
    p = make_profile()
    dash = planner.build_dashboard(p, daily_weighins(START, [85.0, 84.8]), START + timedelta(days=100))
    assert dash["card"]["ended"]
    assert dash["card"]["days_left"] == 0
    assert any("目标日已过" in w["text"] for w in dash["warnings"])
