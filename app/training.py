"""健身房训练计划：力量训练轮换 + 跑步机爬坡，按当天可用时间排成一份可以逐项打勾的清单。

规则（面向体重较大、以减脂为主、每天或隔天去健身房的情况）：
- 力量训练按 A 下肢+核心 → B 上肢推 → C 上肢拉 轮换。只有真的练了（打过勾）才轮到下一个，
  所以不管每天去还是隔天去，同一部位之间至少隔两次训练。
- 连续练满 3 天，第 4 天安排恢复训练（低强度有氧 + 核心 + 拉伸），可以手动换回力量训练。
- 先力量后有氧：力量训练时体力最好、动作更稳，有氧放在后面继续消耗。
- 跑步机：BMI ≥ 30 时只安排爬坡走，不安排跑，减少膝踝冲击；下肢日坡度降一档，避免腿太累动作变形。
- 有氧时长 = 当天总时间 − 热身 − 力量 − 拉伸。时间不够时先去掉可省的辅助动作，尽量给有氧留 25 分钟（时间很紧时按比例少留）。
- 组数次数：主项 4 组 15/12/10/10、每组加一点重量；辅助项 3 组 15/12/12。每组留 1–2 个的余力。
  上次某个动作全部完成，这次建议加一档重量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

TEMPLATE_ORDER = ["A", "B", "C"]
RECOVERY = "R"
STREAK_BEFORE_RECOVERY = 3

WEEKDAY_BUDGET = 60
WEEKEND_BUDGET = 80
BUDGETS = [45, 60, 75, 90]
EXTRA_MIN_BUDGET = 75  # 达到这个时长才加周末的附加动作、主项多一组

WARMUP_MIN = 5
COOLDOWN_MIN = 5
MIN_CARDIO = 15
CARDIO_TARGET = 25  # 排力量动作时尽量给有氧留出的时间，不够就去掉可省的辅助动作
CARDIO_SHARE = 0.45  # 时间很紧时，有氧至少占热身和拉伸以外时间的这个比例
SET_WORK_S = 45  # 一组动作本身大约用时
SETUP_S = 60  # 换器械、调座椅

WALK_ONLY_BMI = 30.0
WEIGHT_STEP = 2.5
STRENGTH_MET = 3.5  # 器械力量训练，中等强度（含组间休息）
STRETCH_MET = 2.3


@dataclass(frozen=True)
class Exercise:
    id: str
    name: str
    equipment: str  # 前端图示的键
    muscles: tuple[str, ...]
    reps: tuple[int, ...]
    rest_s: int
    cue: str
    alt: str
    prio: int = 2  # 1 必做，2 时间不够时可以省，3 只在时间充裕（周末）时做
    weighted: bool = True
    hold: bool = False  # reps 表示秒数（平板支撑之类）
    extra_set: int | None = None  # 时间充裕时主项多做的一组


MAIN_REPS = (15, 12, 10, 10)
ACC_REPS = (15, 12, 12)

EXERCISES = {
    e.id: e
    for e in [
        # A 下肢 + 核心
        Exercise("leg_press", "倒蹬（腿举机）", "leg_press", ("大腿前侧", "臀部"), MAIN_REPS, 90,
                 "脚与肩同宽踩在踏板中间，下放到膝盖约 90°，腰背始终贴住靠垫，蹬起时膝盖不锁死。",
                 "史密斯机深蹲，或高脚杯深蹲", prio=1, extra_set=8),
        Exercise("leg_ext", "坐姿腿屈伸", "leg_ext", ("大腿前侧",), ACC_REPS, 60,
                 "膝盖对准器械转轴，踢到伸直停 1 秒，慢慢放下。",
                 "弹力带腿屈伸，或靠墙静蹲 3×30 秒"),
        Exercise("leg_curl", "坐姿/俯卧腿弯举", "leg_curl", ("大腿后侧",), ACC_REPS, 60,
                 "收紧腹部，用脚跟把垫子往下压到底，回放时控制速度。",
                 "瑞士球腿弯举，或罗马尼亚硬拉（轻重量）"),
        Exercise("hip_abd", "髋外展机", "hip_abd", ("臀部外侧",), (20, 15, 15), 45,
                 "上身坐直，膝盖向外推开，在最外侧停 1 秒。",
                 "弹力带侧向走 3×15 步", prio=2),
        Exercise("calf_raise", "提踵（腿举机或站姿）", "calf_raise", ("小腿",), (20, 15, 15), 45,
                 "前脚掌踩稳，脚跟尽量抬高停 1 秒，再慢慢放到最低。",
                 "台阶上单腿提踵", prio=3),
        Exercise("plank", "平板支撑", "mat", ("核心",), (30, 30, 30), 45,
                 "手肘在肩膀正下方，头、背、臀成一条直线，不塌腰不撅臀。吃力可以跪姿做。",
                 "跪姿平板支撑", prio=1, weighted=False, hold=True),
        # B 上肢 · 推
        Exercise("chest_press", "坐姿推胸", "chest_press", ("胸部", "三头", "肩前侧"), MAIN_REPS, 90,
                 "座椅调到把手与胸中部齐平，肩胛骨向后收紧贴住靠背，推出时不耸肩。",
                 "史密斯机卧推，或哑铃卧推", prio=1, extra_set=8),
        Exercise("shoulder_press", "坐姿推肩", "shoulder_press", ("肩部", "三头"), (12, 12, 10), 75,
                 "背贴靠垫，把手从耳朵高度推到头顶上方，下放到耳朵高度即可。",
                 "坐姿哑铃推肩"),
        Exercise("pec_fly", "蝴蝶机夹胸", "pec_fly", ("胸部",), ACC_REPS, 60,
                 "手肘微屈固定角度，像抱大树一样合拢，在中间停 1 秒挤一下胸。",
                 "龙门架绳索夹胸"),
        Exercise("pushdown", "绳索下压", "cable", ("三头",), ACC_REPS, 60,
                 "大臂贴紧身体不动，只用前臂下压到伸直，最低点把绳子向两边分开。",
                 "器械臂屈伸（坐姿）"),
        Exercise("lateral_raise", "绳索/器械侧平举", "cable", ("肩中束",), (15, 15, 12), 45,
                 "重量要轻，手肘微屈，抬到与肩同高即可，不要耸肩借力。",
                 "哑铃侧平举", prio=3),
        Exercise("dead_bug", "死虫式", "mat", ("核心",), (10, 10, 10), 45,
                 "仰卧，腰贴紧地面，对侧手脚同时慢慢伸远再收回，左右各算 1 次。",
                 "卷腹机 3×15", prio=1, weighted=False),
        # C 上肢 · 拉
        Exercise("lat_pulldown", "高位下拉", "lat_pulldown", ("背阔肌", "二头"), MAIN_REPS, 90,
                 "握距比肩稍宽，挺胸，把横杆拉到锁骨位置，想象用手肘往下往后拉。",
                 "辅助引体向上机", prio=1, extra_set=8),
        Exercise("seated_row", "坐姿划船", "row", ("背部中间", "二头"), (12, 12, 10), 75,
                 "坐直挺胸，把手柄拉向肚脐，肩胛骨向中间夹紧，回放时不弓背。",
                 "器械划船，或单臂哑铃划船"),
        Exercise("rear_delt", "反向飞鸟（蝴蝶机反坐）", "pec_fly", ("肩后束", "上背"), ACC_REPS, 60,
                 "面向靠垫坐，手臂微屈向两侧打开，到身体两侧停 1 秒。",
                 "绳索面拉"),
        Exercise("biceps_curl", "绳索/器械弯举", "cable", ("二头",), ACC_REPS, 60,
                 "大臂贴住身体，只动前臂，顶端停 1 秒，下放要慢。",
                 "哑铃弯举"),
        Exercise("face_pull", "绳索面拉", "cable", ("肩后束", "肩袖"), (15, 15, 12), 45,
                 "绳索调到额头高度，拉向眉心，手肘高于手腕，向两侧分开。",
                 "弹力带面拉", prio=3),
        Exercise("back_ext", "罗马椅背部伸展", "back_ext", ("下背", "臀部"), (12, 12, 12), 45,
                 "髋部卡在垫子边缘，身体下放后用臀部和下背抬起到一条直线，不要过度后仰。",
                 "鸟狗式 3×10（左右各算 1 次）", prio=1, weighted=False),
        # R 恢复
        Exercise("glute_bridge", "臀桥", "mat", ("臀部", "核心"), (15, 15, 15), 30,
                 "仰卧屈膝，脚跟发力把臀部抬到肩、髋、膝一条线，顶端夹臀 1 秒。",
                 "单腿臀桥（更难）", prio=1, weighted=False),
        Exercise("bird_dog", "鸟狗式", "mat", ("核心", "下背"), (10, 10, 10), 30,
                 "四点跪姿，对侧手脚伸直与地面平行，停 2 秒，左右各算 1 次。",
                 "只伸手或只伸腿", prio=1, weighted=False),
    ]
}


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    focus: str
    exercises: tuple[str, ...]
    stretches: str
    cardio: str  # steady / intervals / easy


TEMPLATES = {
    "A": Template("A", "下肢 + 核心", "大腿前后侧、臀部、核心",
                  ("leg_press", "leg_ext", "leg_curl", "hip_abd", "calf_raise", "plank"),
                  "股四头肌、大腿后侧、臀部、小腿，每个部位 30 秒", "steady"),
    "B": Template("B", "上肢 · 推", "胸部、肩部、三头",
                  ("chest_press", "shoulder_press", "pec_fly", "pushdown", "lateral_raise", "dead_bug"),
                  "胸部（门框拉伸）、肩部、三头，每个部位 30 秒", "intervals"),
    "C": Template("C", "上肢 · 拉", "背部、肩后侧、二头",
                  ("lat_pulldown", "seated_row", "rear_delt", "biceps_curl", "face_pull", "back_ext"),
                  "背阔肌、上背、二头、下背（婴儿式），每个部位 30 秒", "intervals"),
    RECOVERY: Template(RECOVERY, "恢复训练", "低强度有氧、核心稳定、全身拉伸",
                       ("glute_bridge", "bird_dog", "dead_bug"),
                       "全身放松拉伸，每个部位 30–45 秒，配合深呼吸", "easy"),
}


@dataclass
class DayState:
    """某一天保存的训练记录。"""

    template: str | None = None  # 手动指定的训练，None 表示按轮换
    budget: int | None = None  # 当天可用分钟数，None 表示按工作日/周末默认
    checked: list[str] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=dict)
    # 以下由服务器在保存时计算，供后面几天推算
    effective_template: str | None = None
    completed: list[str] = field(default_factory=list)  # 全部组都完成的动作
    total: int = 0  # 当天清单的总项数

    @property
    def trained(self) -> bool:
        return bool(self.checked)

    @classmethod
    def from_dict(cls, d: dict | None) -> DayState:
        d = d or {}
        return cls(
            template=d.get("template"),
            budget=d.get("budget"),
            checked=list(d.get("checked") or []),
            weights={k: float(v) for k, v in (d.get("weights") or {}).items()},
            effective_template=d.get("effective_template"),
            completed=list(d.get("completed") or []),
            total=int(d.get("total") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "template": self.template,
            "budget": self.budget,
            "checked": self.checked,
            "weights": self.weights,
            "effective_template": self.effective_template,
            "completed": self.completed,
            "total": self.total,
        }


# ---------- 选择当天的训练 ----------


def choose_template(day: date, history: dict[date, DayState]) -> tuple[str, str]:
    """按之前真正练过的日子推出今天该练什么，返回 (模板, 原因)。"""
    trained = {d: s for d, s in history.items() if d < day and s.trained and s.effective_template}
    streak = 0
    d = day - timedelta(days=1)
    while d in trained:
        streak += 1
        d -= timedelta(days=1)
    if streak >= STREAK_BEFORE_RECOVERY:
        return RECOVERY, f"已经连续练了 {streak} 天，今天做恢复训练，让肌肉和关节缓一缓"
    last = next(
        ((d, trained[d].effective_template) for d in sorted(trained, reverse=True)
         if trained[d].effective_template in TEMPLATE_ORDER),
        None,
    )
    if last is None:
        return "A", "第一次训练，从下肢开始：大肌群消耗最多"
    last_day, last_key = last
    nxt = TEMPLATE_ORDER[(TEMPLATE_ORDER.index(last_key) + 1) % len(TEMPLATE_ORDER)]
    gap = (day - last_day).days
    when = "昨天" if gap == 1 else f"{gap} 天前（{last_day.month}月{last_day.day}日）"
    return nxt, f"{when}练了「{TEMPLATES[last_key].title}」，今天轮到「{TEMPLATES[nxt].title}」"


def default_budget(day: date) -> int:
    return WEEKEND_BUDGET if day.weekday() >= 5 else WEEKDAY_BUDGET


# ---------- 力量训练 ----------


def _exercise_seconds(reps: list[int], ex: Exercise) -> int:
    work = sum(reps) if ex.hold else SET_WORK_S * len(reps)
    return SETUP_S + work + ex.rest_s * (len(reps) - 1)


def _progression(ex: Exercise, day: date, history: dict[date, DayState]) -> dict:
    """上次用的重量和这次的建议。"""
    if not ex.weighted:
        return {"last": None, "suggest": None, "hint": "自重动作：做不完就减少次数，轻松完成就放慢速度"}
    for d in sorted((d for d in history if d < day), reverse=True):
        s = history[d]
        if ex.id in s.weights:
            last = s.weights[ex.id]
            if ex.id in s.completed:
                return {"last": last, "last_day": d.isoformat(), "suggest": last + WEIGHT_STEP,
                        "hint": f"上次 {last:g} kg 全部完成，今天加一档试试"}
            return {"last": last, "last_day": d.isoformat(), "suggest": last,
                    "hint": f"上次 {last:g} kg 没做完，今天保持这个重量"}
    return {"last": None, "suggest": None,
            "hint": "第一次做：选一个能做 15 个、最后 2–3 个比较吃力的重量"}


def _strength(t: Template, budget: int, day: date, history: dict[date, DayState]) -> list[dict]:
    roomy = budget >= EXTRA_MIN_BUDGET
    picked = []
    for ex_id in t.exercises:
        ex = EXERCISES[ex_id]
        if ex.prio == 3 and not roomy:
            continue
        reps = list(ex.reps) + ([ex.extra_set] if roomy and ex.extra_set else [])
        picked.append((ex, reps))

    def seconds() -> int:
        return sum(_exercise_seconds(r, e) for e, r in picked)

    # 时间不够时从后往前去掉可省的动作
    available = budget - WARMUP_MIN - COOLDOWN_MIN
    target = min(CARDIO_TARGET, max(MIN_CARDIO, available * CARDIO_SHARE))
    for ex, reps in reversed(list(picked)):
        if available * 60 - seconds() >= target * 60:
            break
        if ex.prio >= 2:
            picked.remove((ex, reps))

    out = []
    for ex, reps in picked:
        out.append({
            "id": ex.id,
            "name": ex.name,
            "equipment": ex.equipment,
            "muscles": list(ex.muscles),
            "sets": [{"id": f"{ex.id}:{i}", "reps": r} for i, r in enumerate(reps)],
            "hold": ex.hold,
            "rest_s": ex.rest_s,
            "minutes": round(_exercise_seconds(reps, ex) / 60, 1),
            "cue": ex.cue,
            "alt": ex.alt,
            "main": ex.prio == 1 and ex.weighted,
            "weighted": ex.weighted,
            "progression": _progression(ex, day, history),
        })
    return out


# ---------- 跑步机 ----------


def treadmill_kcal(minutes: float, speed_kmh: float, incline_pct: float, weight: float) -> float:
    """ACSM 代谢公式估算消耗（走路 / 跑步两套系数）。扶扶手时实际会少 20–30%。"""
    v = speed_kmh * 1000 / 60  # m/min
    g = incline_pct / 100
    vo2 = (0.1 * v + 1.8 * v * g + 3.5) if speed_kmh < 6.5 else (0.2 * v + 0.9 * v * g + 3.5)
    return vo2 * weight / 1000 * 5 * minutes


def _seg(minutes: float, speed: float, incline: float, kind: str, label: str) -> dict:
    return {"minutes": minutes, "speed": speed, "incline": incline, "kind": kind, "label": label}


def treadmill_plan(style: str, minutes: int, walk_only: bool) -> tuple[list[dict], list[dict], str]:
    """返回 (分段, 打勾项, 说明)。每个打勾项对应一段或一轮间歇。"""
    warm = _seg(2, 4.5, 5, "warm", "适应")
    cool = _seg(3, 4.0, 2, "cool", "放松")
    main = max(minutes - warm["minutes"] - cool["minutes"], 5)
    segs = [warm]
    items = [{"id": "cardio:warm", "title": "适应 2 分钟", "detail": "4.5 km/h · 坡度 5%"}]

    if style == "steady":
        segs.append(_seg(main, 4.5, 10, "steady", "稳定爬坡"))
        items.append({"id": "cardio:main", "title": f"稳定爬坡 {main} 分钟", "detail": "4.5 km/h · 坡度 10%"})
        note = "练完腿后大腿已经很累，坡度降到 10%，保持匀速，心率稳定在燃脂区间。"
    elif style == "easy":
        segs.append(_seg(main, 4.8, 8, "steady", "轻松快走"))
        items.append({"id": "cardio:main", "title": f"轻松快走 {main} 分钟", "detail": "4.8 km/h · 坡度 8%"})
        note = "恢复日强度要低：能边走边说完整的句子。时间长一点，消耗一样可观。"
    else:
        if walk_only:
            work, ease = _seg(4, 4.5, 15, "work", "爬坡"), _seg(2, 5.0, 8, "ease", "缓坡快走")
            note = "保留你习惯的 15% 爬坡，每 4 分钟降到 8% 走 2 分钟：心率有起伏，消耗更多，小腿和跟腱也能喘口气。"
        else:
            work, ease = _seg(3, 5.0, 12, "work", "快走爬坡"), _seg(2, 7.5, 1, "ease", "慢跑")
            note = "体重已降到适合慢跑的范围，用快走爬坡和慢跑交替。慢跑时步子小、落地轻。"
        cycle = work["minutes"] + ease["minutes"]
        rounds = max(int(main // cycle), 1)
        rest = main - rounds * cycle
        for i in range(rounds):
            segs += [dict(work), dict(ease)]
            items.append({
                "id": f"cardio:r{i}",
                "title": f"第 {i + 1} 轮",
                "detail": f"{work['minutes']:g} 分钟 {work['speed']:g} km/h · 坡度 {work['incline']:g}%"
                          f" → {ease['minutes']:g} 分钟 {ease['speed']:g} km/h · 坡度 {ease['incline']:g}%",
            })
        if rest >= 4:
            segs.append(_seg(rest, 4.5, 12, "steady", "稳定爬坡"))
            items.append({"id": "cardio:tail", "title": f"稳定爬坡 {rest:g} 分钟", "detail": "4.5 km/h · 坡度 12%"})
        elif rest > 0:  # 零头太短不单独成段，加到适应段
            warm["minutes"] += rest
            items[0]["title"] = f"适应 {warm['minutes']:g} 分钟"
    segs.append(cool)
    items.append({"id": "cardio:cool", "title": "放松 3 分钟", "detail": "4.0 km/h · 坡度 2%"})
    return segs, items, note


# ---------- 组装 ----------


def build_plan(
    day: date,
    history: dict[date, DayState],
    *,
    age: int,
    weight: float,
    height_cm: float,
) -> dict:
    state = history.get(day) or DayState()
    auto_key, reason = choose_template(day, history)
    key = state.template if state.template in TEMPLATES else auto_key
    if key != auto_key:
        reason = f"按轮换今天是「{TEMPLATES[auto_key].title}」，已手动换成「{TEMPLATES[key].title}」"
    t = TEMPLATES[key]
    budget = state.budget if state.budget and 30 <= state.budget <= 150 else default_budget(day)

    strength = _strength(t, budget, day, history)
    strength_min = round(sum(e["minutes"] for e in strength))
    cardio_min = max(budget - WARMUP_MIN - COOLDOWN_MIN - strength_min, MIN_CARDIO)

    bmi = weight / (height_cm / 100) ** 2
    walk_only = bmi >= WALK_ONLY_BMI
    segments, cardio_items, cardio_note = treadmill_plan(t.cardio, cardio_min, walk_only)

    hr_max = round(208 - 0.7 * age)  # Tanaka 公式
    zone2 = [round(hr_max * 0.6), round(hr_max * 0.7)]
    hard = [round(hr_max * 0.7), round(hr_max * 0.8)]

    warmup_items = [
        {"id": "warm:walk", "title": "跑步机快走 3 分钟", "detail": "4.5 km/h · 坡度 5%，身体微微发热即可"},
        {"id": "warm:mobility", "title": "动态热身 2 分钟",
         "detail": "手臂绕环、髋关节绕环、徒手深蹲 10 个" if key == "A" else "手臂绕环、肩胛骨开合、弹力带拉开 15 次"},
    ]
    cool_items = [{"id": "cool:stretch", "title": f"拉伸 {COOLDOWN_MIN} 分钟", "detail": t.stretches}]

    all_ids = [i["id"] for i in warmup_items + cardio_items + cool_items]
    all_ids += [s["id"] for e in strength for s in e["sets"]]
    checked = [c for c in state.checked if c in set(all_ids)]

    kcal_cardio = sum(treadmill_kcal(s["minutes"], s["speed"], s["incline"], weight) for s in segments)
    kcal_warm = treadmill_kcal(3, 4.5, 5, weight)
    kcal_strength = STRENGTH_MET * weight * strength_min / 60
    kcal_stretch = STRETCH_MET * weight * (COOLDOWN_MIN + 2) / 60

    return {
        "day": day.isoformat(),
        "weekday": day.weekday(),
        "weekend": day.weekday() >= 5,
        "template": key,
        "auto_template": auto_key,
        "title": t.title,
        "focus": t.focus,
        "reason": reason,
        "budget": budget,
        "budget_options": BUDGETS,
        "default_budget": default_budget(day),
        "templates": [{"key": k, "title": v.title} for k, v in TEMPLATES.items()],
        "blocks": {
            "warmup": {"minutes": WARMUP_MIN, "items": warmup_items},
            "strength": {"minutes": strength_min, "exercises": strength},
            "cardio": {
                "minutes": cardio_min,
                "segments": segments,
                "items": cardio_items,
                "note": cardio_note,
                "walk_only": walk_only,
                "walk_only_until": round(WALK_ONLY_BMI * (height_cm / 100) ** 2, 1),
            },
            "cooldown": {"minutes": COOLDOWN_MIN, "items": cool_items},
        },
        "heart_rate": {"max": hr_max, "zone2": zone2, "hard": hard},
        "kcal": {
            "total": round(kcal_cardio + kcal_warm + kcal_strength + kcal_stretch),
            "cardio": round(kcal_cardio + kcal_warm),
            "strength": round(kcal_strength),
        },
        "bmi": round(bmi, 1),
        "state": {
            "template": state.template,
            "budget": state.budget,
            "checked": checked,
            "weights": state.weights,
        },
        "progress": {"done": len(checked), "total": len(all_ids)},
    }


def completed_exercises(plan: dict, checked: list[str]) -> list[str]:
    done = set(checked)
    return [e["id"] for e in plan["blocks"]["strength"]["exercises"] if all(s["id"] in done for s in e["sets"])]


def recent_days(day: date, history: dict[date, DayState], n: int = 14) -> list[dict]:
    """最近 n 天（含当天）的训练情况，用于周视图。"""
    out = []
    for i in range(n - 1, -1, -1):
        d = day - timedelta(days=i)
        s = history.get(d)
        out.append({
            "day": d.isoformat(),
            "template": s.effective_template if s and s.trained else None,
            "done": len(s.checked) if s else 0,
            "total": s.total if s else 0,
        })
    return out
