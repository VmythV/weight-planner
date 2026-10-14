"""减重计划算法。

- 趋势体重：对最近 21 天的称重做加权线性拟合，滤掉水分、进食造成的日常波动，
  且体重持续下降时不会像移动平均那样滞后。
- 能量模型：Mifflin-St Jeor 基础代谢 × 活动系数，约 7700 kcal 对应 1 kg 体重。
- 计划曲线：体重下降后消耗也随之下降，每日摄入固定时体重按指数曲线趋近平衡点。
  反解出"目标日恰好到达目标体重"的每日摄入，得到前快后慢的平滑曲线。
- 每周校准：每 7 天用当时的趋势体重和剩余天数重新求解建议摄入。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta

KCAL_PER_KG = 7700.0
TREND_WINDOW_DAYS = 21
TREND_HALF_LIFE_DAYS = 10.0
MAX_WEEKLY_LOSS_PCT = 1.0  # 每周减重不超过体重的 1%
DANGER_WEEKLY_LOSS_PCT = 1.5  # 超过这个速度提示升级为"过快"
INTAKE_FLOOR = {"male": 1500.0, "female": 1200.0}
MIN_BMI = 18.5
BAND_KG = 0.5  # 计划曲线上下的容差带
CHECKIN_DAYS = 7
MAX_PLAN_DAYS = 3650
PROJECTION_TAIL_DAYS = 180
PLATEAU_WINDOW_DAYS = 14
PLATEAU_MIN_POINTS = 5
PLATEAU_MIN_LOSS_PER_WEEK = 0.1
STALE_DAYS = 3
PROTEIN_G_PER_KG = 1.6  # 按目标体重计算
FAT_RATIO = 0.25
WALK_KCAL_PER_KG_KM = 0.5
STEP_LENGTH_M = 0.7

ACTIVITY_FACTORS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "active": 1.725,
}


@dataclass(frozen=True)
class Profile:
    sex: str  # "male" | "female"
    age: int
    height_cm: float
    activity: str
    target_weight: float
    plan_start: date
    target_date: date
    exercise_kcal: float = 0.0  # 每天计划的额外运动消耗
    start_weight: float | None = None  # 为空时取计划开始日的趋势体重

    @property
    def intake_floor(self) -> float:
        return INTAKE_FLOOR[self.sex]


@dataclass(frozen=True)
class Weighin:
    measured_at: datetime
    weight_kg: float


# ---------- 能量模型 ----------


def bmr(weight: float, p: Profile) -> float:
    sex_offset = 5.0 if p.sex == "male" else -161.0
    return 10.0 * weight + 6.25 * p.height_cm - 5.0 * p.age + sex_offset


def tdee(weight: float, p: Profile) -> float:
    return bmr(weight, p) * ACTIVITY_FACTORS[p.activity]


def bmi(weight: float, p: Profile) -> float:
    return weight / (p.height_cm / 100.0) ** 2


def _energy_slope(p: Profile) -> float:
    """tdee(W) = slope·W + tdee(0)，即每公斤体重对应的每日消耗。"""
    return 10.0 * ACTIVITY_FACTORS[p.activity]


@dataclass(frozen=True)
class Curve:
    """每日摄入固定时的体重曲线：W(t) = w_eq + (w0 - w_eq)·e^(-k·t)。"""

    start: date
    w0: float
    intake: float
    w_eq: float  # 该摄入下最终会稳定到的体重
    k: float

    def weight_at(self, day: date) -> float:
        t = (day - self.start).days
        return self.w_eq + (self.w0 - self.w_eq) * math.exp(-self.k * t)

    @property
    def initial_weekly_loss_pct(self) -> float:
        """起始时每周减重占体重的百分比；曲线前快后慢，这是全程最快的速度。"""
        return 7.0 * self.k * (self.w0 - self.w_eq) / self.w0 * 100.0

    def days_to_reach(self, target: float) -> float | None:
        if self.w0 == self.w_eq:
            return 0.0 if target == self.w0 else None
        ratio = (target - self.w_eq) / (self.w0 - self.w_eq)
        if not 0.0 < ratio <= 1.0:
            return None
        return -math.log(ratio) / self.k


def curve_for_intake(w0: float, start: date, intake: float, p: Profile) -> Curve:
    slope = _energy_slope(p)
    w_eq = (intake - tdee(0.0, p) - p.exercise_kcal) / slope
    return Curve(start=start, w0=w0, intake=intake, w_eq=w_eq, k=slope / KCAL_PER_KG)


def solve_intake(w0: float, target: float, days: int, p: Profile) -> float:
    """反解每日摄入，使体重在 days 天后恰好到达 target。"""
    if days <= 0:
        raise ValueError("days must be positive")
    slope = _energy_slope(p)
    q = math.exp(-slope / KCAL_PER_KG * days)
    w_eq = (target - w0 * q) / (1.0 - q)
    return slope * w_eq + tdee(0.0, p) + p.exercise_kcal


# ---------- 趋势体重 ----------


def daily_weights(weighins: list[Weighin]) -> list[tuple[date, float]]:
    """每天取最早的一次称重（晨起空腹最稳定）。"""
    by_day: dict[date, Weighin] = {}
    for w in weighins:
        day = w.measured_at.date()
        if day not in by_day or w.measured_at < by_day[day].measured_at:
            by_day[day] = w
    return [(day, by_day[day].weight_kg) for day in sorted(by_day)]


def trend_series(daily: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """每天的趋势体重：最近 21 天的称重按 10 天半衰期加权，拟合直线后取当天的值。

    指数移动平均在体重持续下降时会偏高约 9 天的下降量（按计划减重时约 1 kg），
    线性拟合没有这种滞后；数据太少（不足 3 天或跨度不足 2 天）时退化为加权平均。
    """
    out: list[tuple[date, float]] = []
    for i, (day, _) in enumerate(daily):
        window = [(d, w) for d, w in daily[: i + 1] if (day - d).days < TREND_WINDOW_DAYS]
        xs = [(d - day).days for d, _ in window]  # 相对当天的天数，≤ 0
        ys = [w for _, w in window]
        ws = [0.5 ** (-x / TREND_HALF_LIFE_DAYS) for x in xs]
        total = sum(ws)
        mean_x = sum(w * x for w, x in zip(ws, xs)) / total
        mean_y = sum(w * y for w, y in zip(ws, ys)) / total
        if len(window) < 3 or xs[-1] - xs[0] < 2:
            out.append((day, mean_y))
            continue
        sxx = sum(w * (x - mean_x) ** 2 for w, x in zip(ws, xs))
        slope = sum(w * (x - mean_x) * (y - mean_y) for w, x, y in zip(ws, xs, ys)) / sxx
        out.append((day, mean_y - slope * mean_x))
    return out


def value_on_or_before(series: list[tuple[date, float]], day: date) -> float | None:
    i = bisect_right([d for d, _ in series], day)
    return series[i - 1][1] if i else None


def plateau_rate(trend: list[tuple[date, float]], plan_start: date) -> float | None:
    """近 14 天趋势体重的每周变化（kg，负数为下降）；数据不足时返回 None。"""
    if not trend:
        return None
    window_start = trend[-1][0] - timedelta(days=PLATEAU_WINDOW_DAYS)
    if window_start < plan_start:
        return None
    points = [(d, w) for d, w in trend if d >= window_start]
    if len(points) < PLATEAU_MIN_POINTS:
        return None
    xs = [(d - window_start).days for d, _ in points]
    ys = [w for _, w in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
    return slope * 7.0


# ---------- 计划与安全检查 ----------


@dataclass(frozen=True)
class PlanCheck:
    days: int
    intake: float
    weekly_loss_pct: float
    below_floor: bool
    too_fast: bool
    extra_exercise_needed: float  # 保持目标日期不变时，摄入回到下限所需的额外运动
    suggested_date: date | None
    target_bmi: float


def _is_safe(w0: float, days: int, p: Profile) -> bool:
    intake = solve_intake(w0, p.target_weight, days, p)
    curve = curve_for_intake(w0, p.plan_start, intake, p)
    return intake >= p.intake_floor and curve.initial_weekly_loss_pct <= MAX_WEEKLY_LOSS_PCT


def earliest_safe_date(w0: float, p: Profile) -> date | None:
    """满足摄入下限和减重速度上限的最早目标日期；永远无法满足时返回 None。"""
    lo, hi = 1, MAX_PLAN_DAYS
    if not _is_safe(w0, hi, p):
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_safe(w0, mid, p):
            hi = mid
        else:
            lo = mid + 1
    return p.plan_start + timedelta(days=lo)


def check_plan(w0: float, p: Profile) -> PlanCheck:
    days = (p.target_date - p.plan_start).days
    intake = solve_intake(w0, p.target_weight, days, p)
    curve = curve_for_intake(w0, p.plan_start, intake, p)
    losing = p.target_weight < w0
    below_floor = losing and intake < p.intake_floor
    too_fast = losing and curve.initial_weekly_loss_pct > MAX_WEEKLY_LOSS_PCT
    return PlanCheck(
        days=days,
        intake=intake,
        weekly_loss_pct=curve.initial_weekly_loss_pct if losing else 0.0,
        below_floor=below_floor,
        too_fast=too_fast,
        extra_exercise_needed=p.intake_floor - intake if below_floor else 0.0,
        suggested_date=earliest_safe_date(w0, p) if below_floor or too_fast else None,
        target_bmi=bmi(p.target_weight, p),
    )


@dataclass(frozen=True)
class Checkin:
    day: date
    trend_weight: float
    days_left: int
    required_intake: float  # 按期达成所需
    recommended_intake: float  # 套用安全下限后的建议值
    reached: bool


def weekly_checkins(
    w0: float, trend: list[tuple[date, float]], p: Profile, today: date
) -> list[Checkin]:
    losing = p.target_weight < w0
    last = min(today, p.target_date - timedelta(days=1))
    out: list[Checkin] = []
    day = p.plan_start
    while day <= last:
        weight = w0 if day == p.plan_start else (value_on_or_before(trend, day) or w0)
        days_left = (p.target_date - day).days
        reached = losing and weight <= p.target_weight
        if reached:
            required = recommended = tdee(weight, p) + p.exercise_kcal
        else:
            required = solve_intake(weight, p.target_weight, days_left, p)
            recommended = max(required, p.intake_floor) if losing else required
        out.append(Checkin(day, weight, days_left, required, recommended, reached))
        day += timedelta(days=CHECKIN_DAYS)
    return out


# ---------- 汇总给前端 ----------


def _r2(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _kcal(value: float | None) -> int | None:
    return None if value is None else round(value)


def build_dashboard(p: Profile | None, weighins: list[Weighin], today: date) -> dict:
    if p is None:
        return {"ready": False, "reason": "no_profile"}

    daily = daily_weights(weighins)
    trend = trend_series(daily)
    w0 = p.start_weight or value_on_or_before(trend, p.plan_start) or (trend[0][1] if trend else None)
    if w0 is None:
        return {"ready": False, "reason": "no_data"}

    losing = p.target_weight < w0
    check = check_plan(w0, p)
    plan_curve = curve_for_intake(w0, p.plan_start, check.intake, p)
    checkins = weekly_checkins(w0, trend, p, today)
    initial_rec = max(check.intake, p.intake_floor) if losing else check.intake
    rec = checkins[-1].recommended_intake if checkins else initial_rec

    latest_day, latest_trend = trend[-1] if trend else (p.plan_start, w0)
    reached = losing and latest_trend <= p.target_weight
    ended = today > p.target_date
    if reached or ended:
        rec = tdee(latest_trend, p) + p.exercise_kcal  # 改为维持热量

    proj_start = max(latest_day, p.plan_start)
    projection = curve_for_intake(latest_trend, proj_start, rec, p)
    reach_date = None
    if losing and not reached and not ended:
        reach_days = projection.days_to_reach(p.target_weight)
        if reach_days is not None:
            reach_date = proj_start + timedelta(days=math.ceil(reach_days))

    def clamp_to_plan(day: date) -> date:
        return min(max(day, p.plan_start), p.target_date)

    diff = None
    if not trend or latest_day < p.plan_start:
        status = "not_started"
    elif not losing:
        status = "not_applicable"
    else:
        diff = latest_trend - plan_curve.weight_at(clamp_to_plan(latest_day))
        if reached:
            status = "reached"
        elif diff <= -BAND_KG:
            status = "ahead"
        elif diff <= 0:
            status = "on_track"
        elif diff <= BAND_KG:
            status = "slightly_behind"
        else:
            status = "behind"

    burn_bmr = bmr(latest_trend, p)
    burn_daily = tdee(latest_trend, p)
    burn_total = burn_daily + p.exercise_kcal
    protein = PROTEIN_G_PER_KG * p.target_weight
    fat = rec * FAT_RATIO / 9.0
    carbs = max(0.0, (rec - protein * 4.0 - fat * 9.0) / 4.0)
    steps = 0
    if p.exercise_kcal > 0:
        km = p.exercise_kcal / (WALK_KCAL_PER_KG_KM * latest_trend)
        steps = round(km * 1000.0 / STEP_LENGTH_M)
    progress = None
    if losing:
        progress = max(0.0, min(100.0, (w0 - latest_trend) / (w0 - p.target_weight) * 100.0))

    card = {
        "status": status,
        "plan_weight_today": _r2(plan_curve.weight_at(clamp_to_plan(today))),
        "trend_weight": _r2(latest_trend),
        "trend_date": latest_day.isoformat() if trend else None,
        "latest_weight": _r2(daily[-1][1]) if daily else None,
        "latest_date": daily[-1][0].isoformat() if daily else None,
        "diff": _r2(diff),
        "intake": _kcal(rec),
        "burn_total": _kcal(burn_total),
        "burn_bmr": _kcal(burn_bmr),
        "burn_activity": _kcal(burn_daily - burn_bmr),
        "burn_exercise": _kcal(p.exercise_kcal),
        "deficit": _kcal(burn_total) - _kcal(rec),  # 先取整再相减，页面上的数字能对上
        "protein_g": round(protein),
        "fat_g": round(fat),
        "carbs_g": round(carbs),
        "exercise_steps": steps,
        "days_left": max(0, (p.target_date - today).days),
        "lost_kg": _r2(w0 - latest_trend),
        "remaining_kg": _r2(max(0.0, latest_trend - p.target_weight)),
        "progress_pct": None if progress is None else round(progress, 1),
        "projected_reach_date": reach_date.isoformat() if reach_date else None,
        "projected_weight_at_target": None if ended else _r2(projection.weight_at(p.target_date)),
        "reached": reached,
        "ended": ended,
    }

    warnings: list[dict] = []

    def warn(level: str, text: str) -> None:
        warnings.append({"level": level, "text": text})

    suggestion = f"建议把目标日期延后到 {check.suggested_date}。" if check.suggested_date else ""
    if check.too_fast:
        severe = check.weekly_loss_pct > DANGER_WEEKLY_LOSS_PCT
        warn(
            "danger" if severe else "warn",
            f"计划减重速度{'过快' if severe else '偏快'}：开始阶段每周约减体重的 {check.weekly_loss_pct:.2f}%"
            f"（建议不超过 {MAX_WEEKLY_LOSS_PCT:.0f}%）。{suggestion}",
        )
    if check.below_floor:
        text = (
            f"按期达成需要每天只摄入 {check.intake:.0f} kcal，低于安全下限 {p.intake_floor:.0f} kcal。"
            f"可以每天额外运动约 {check.extra_exercise_needed:.0f} kcal 来补足，"
        )
        if check.suggested_date:
            text += f"或把目标日期延后到 {check.suggested_date}。"
        else:
            text += "但目标体重下的维持热量本身就低于下限，建议调高目标体重或提高活动量。"
        warn("danger", text)
    if check.target_bmi < MIN_BMI:
        warn("warn", f"目标体重对应的 BMI 为 {check.target_bmi:.1f}，低于 {MIN_BMI}，属于偏瘦，建议调高目标。")
    if not losing:
        warn("info", "目标体重不低于起始体重，当前按维持或增重计算，安全检查不适用。")
    if (
        checkins
        and not check.below_floor
        and not checkins[-1].reached
        and checkins[-1].required_intake < p.intake_floor
        and not reached
        and not ended
    ):
        last = checkins[-1]
        text = (
            f"按当前进度，剩余 {last.days_left} 天需要每天只摄入 {last.required_intake:.0f} kcal "
            f"才能按期达成，已按安全下限 {p.intake_floor:.0f} kcal 给出建议。"
        )
        if reach_date:
            text += f"按此摄入预计 {reach_date} 达到目标。"
        warn("warn", text)
    rate = plateau_rate(trend, p.plan_start)
    if losing and not reached and not ended and rate is not None and rate > -PLATEAU_MIN_LOSS_PER_WEEK:
        warn(
            "warn",
            f"近 {PLATEAU_WINDOW_DAYS} 天趋势体重几乎没有下降（每周 {rate:+.2f} kg），可能进入平台期："
            "检查实际摄入是否超出建议值、适当增加日常活动，或安排 1–2 周维持热量后再继续。",
        )
    if daily and (today - daily[-1][0]).days >= STALE_DAYS:
        warn("info", f"已经 {(today - daily[-1][0]).days} 天没有称重了，每天早上称一次，趋势才准确。")
    if reached:
        warn("success", f"已达到目标体重！建议按维持热量约 {rec:.0f} kcal 摄入，巩固成果。")
    elif ended:
        warn(
            "info",
            f"目标日已过：当前趋势体重 {latest_trend:.1f} kg，目标 {p.target_weight:.1f} kg。"
            "可以在「计划设置」里制定新计划。",
        )

    # 图表序列：从最早的称重或计划开始日，到目标日（预测更晚到达时向后延伸）
    first_day = min(p.plan_start, daily[0][0]) if daily else p.plan_start
    last_day = max(p.target_date, daily[-1][0]) if daily else p.target_date
    if reach_date and reach_date > last_day:
        last_day = min(reach_date, p.target_date + timedelta(days=PROJECTION_TAIL_DAYS))
    daily_map = dict(daily)
    trend_map = dict(trend)
    checkin_days = [c.day for c in checkins]

    def intake_for_day(day: date) -> float:
        if day > today:
            return rec
        i = bisect_right(checkin_days, day)
        return checkins[i - 1].recommended_intake if i else initial_rec

    show_projection = losing and not reached and not ended
    projection_end = max(p.target_date, reach_date) if reach_date else p.target_date
    series: dict[str, list] = {
        key: []
        for key in (
            "dates", "plan", "band_low", "band_high", "actual", "trend",
            "projection", "burn", "intake", "deficit",
        )
    }
    day = first_day
    while day <= last_day:
        in_plan = p.plan_start <= day <= p.target_date
        plan_weight = plan_curve.weight_at(day) if in_plan else None
        series["dates"].append(day.isoformat())
        series["plan"].append(_r2(plan_weight))
        series["band_low"].append(_r2(plan_weight - BAND_KG) if in_plan else None)
        series["band_high"].append(_r2(plan_weight + BAND_KG) if in_plan else None)
        series["actual"].append(_r2(daily_map.get(day)))
        series["trend"].append(_r2(trend_map.get(day)))
        on_projection = show_projection and proj_start <= day <= projection_end
        series["projection"].append(_r2(projection.weight_at(day)) if on_projection else None)
        if in_plan:
            if day <= latest_day:
                ref_weight = value_on_or_before(trend, day) or w0
            else:
                ref_weight = projection.weight_at(day)
            burn = _kcal(tdee(ref_weight, p) + p.exercise_kcal)
            day_intake = _kcal(intake_for_day(day))
            series["burn"].append(burn)
            series["intake"].append(day_intake)
            series["deficit"].append(burn - day_intake)
        else:
            for key in ("burn", "intake", "deficit"):
                series[key].append(None)
        day += timedelta(days=1)

    return {
        "ready": True,
        "today": today.isoformat(),
        "plan": {
            "start_weight": _r2(w0),
            "target_weight": p.target_weight,
            "plan_start": p.plan_start.isoformat(),
            "target_date": p.target_date.isoformat(),
            "days": check.days,
            "intake": _kcal(check.intake),
            "weekly_loss_pct": round(check.weekly_loss_pct, 2),
            "suggested_date": check.suggested_date.isoformat() if check.suggested_date else None,
            "extra_exercise_needed": _kcal(check.extra_exercise_needed),
            "target_bmi": round(check.target_bmi, 1),
            "intake_floor": _kcal(p.intake_floor),
            "losing": losing,
        },
        "card": card,
        "warnings": warnings,
        "checkins": [
            {
                "day": c.day.isoformat(),
                "trend_weight": _r2(c.trend_weight),
                "days_left": c.days_left,
                "required_intake": _kcal(c.required_intake),
                "recommended_intake": _kcal(c.recommended_intake),
                "reached": c.reached,
            }
            for c in checkins
        ],
        "series": series,
    }
