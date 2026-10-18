from datetime import date, timedelta

from fastapi.testclient import TestClient

from app import training as t
from app.auth import AuthSettings
from app.db import Store
from app.main import create_app

D = date(2026, 10, 19)  # 周一
KW = dict(age=35, weight=95.0, height_cm=175)


def trained(key: str) -> t.DayState:
    return t.DayState(checked=["x"], effective_template=key)


def plan_ids(plan: dict) -> list[str]:
    b = plan["blocks"]
    ids = [i["id"] for k in ("warmup", "cardio", "cooldown") for i in b[k]["items"]]
    return ids + [s["id"] for e in b["strength"]["exercises"] for s in e["sets"]]


def test_rotation_advances_only_on_trained_days():
    assert t.choose_template(D, {})[0] == "A"
    h = {D - timedelta(days=3): trained("A")}
    assert t.choose_template(D, h)[0] == "B"
    h[D - timedelta(days=2)] = t.DayState()  # 打开过页面但没练，不算
    assert t.choose_template(D, h)[0] == "B"
    h[D - timedelta(days=1)] = trained("B")
    assert t.choose_template(D, h)[0] == "C"


def test_recovery_after_three_days_in_a_row():
    h = {D - timedelta(days=i): trained(k) for i, k in zip((3, 2, 1), "ABC")}
    key, reason = t.choose_template(D, h)
    assert key == "R" and "3 天" in reason
    h[D] = trained("R")
    # 恢复日不打断轮换：下一次接着上一个力量训练往后排
    assert t.choose_template(D + timedelta(days=2), h)[0] == "A"


def test_budget_shapes_the_session():
    weekday = t.build_plan(D, {}, **KW)
    weekend = t.build_plan(D + timedelta(days=5), {}, **KW)
    assert weekday["budget"] == 60 and weekend["budget"] == 80
    for p in (weekday, weekend):
        b = p["blocks"]
        used = b["warmup"]["minutes"] + b["strength"]["minutes"] + b["cardio"]["minutes"] + b["cooldown"]["minutes"]
        assert abs(used - p["budget"]) <= 1
        assert b["cardio"]["minutes"] >= 25
        assert sum(s["minutes"] for s in b["cardio"]["segments"]) == b["cardio"]["minutes"]
    main = weekend["blocks"]["strength"]["exercises"][0]
    assert main["main"] and len(main["sets"]) == 5  # 周末主项多一组
    assert len(weekend["blocks"]["strength"]["exercises"]) > len(weekday["blocks"]["strength"]["exercises"])
    short = t.build_plan(D, {D: t.DayState(budget=45)}, **KW)
    assert len(short["blocks"]["strength"]["exercises"]) >= 3


def test_walk_only_until_bmi_below_30():
    heavy = t.build_plan(D, {D: t.DayState(template="B")}, **KW)
    assert heavy["blocks"]["cardio"]["walk_only"]
    assert max(s["speed"] for s in heavy["blocks"]["cardio"]["segments"]) < 6.5
    light = t.build_plan(D, {D: t.DayState(template="B")}, age=35, weight=85, height_cm=175)
    assert not light["blocks"]["cardio"]["walk_only"]
    assert max(s["speed"] for s in light["blocks"]["cardio"]["segments"]) >= 6.5


def test_progression_suggests_next_weight():
    prev = D - timedelta(days=3)
    h = {
        prev: t.DayState(checked=["x"], effective_template="A", weights={"leg_press": 40}, completed=["leg_press"]),
        D: t.DayState(template="A"),
    }
    ex = t.build_plan(D, h, **KW)["blocks"]["strength"]["exercises"][0]
    assert ex["progression"]["suggest"] == 42.5
    h[prev].completed = []
    ex = t.build_plan(D, h, **KW)["blocks"]["strength"]["exercises"][0]
    assert ex["progression"]["suggest"] == 40


def test_treadmill_kcal():
    # 4.5 km/h、15% 坡度、100 kg：ACSM 约 31 ml/kg/min → 约 15.6 kcal/min
    assert 15 < t.treadmill_kcal(1, 4.5, 15, 100) < 16.5


def test_training_api(tmp_path):
    c = TestClient(create_app(Store(tmp_path / "t.db"), AuthSettings()))
    assert c.get("/api/training").json() == {"ready": False}
    c.put("/api/profile", json={
        "sex": "male", "age": 35, "height_cm": 175, "activity": "moderate", "target_weight": 75,
        "plan_start": "2026-09-01", "target_date": "2027-03-03", "exercise_kcal": 0,
    })
    c.post("/api/weighins", json={"weight_kg": 95})
    day = D.isoformat()
    plan = c.get("/api/training", params={"day": day}).json()
    assert plan["ready"] and plan["template"] == "A" and len(plan["recent"]) == 14
    ids = plan_ids(plan)
    r = c.put(f"/api/training/{day}", json={"checked": ids + ["bogus"], "weights": {"leg_press": 60}})
    assert r.status_code == 200
    saved = r.json()
    assert saved["progress"]["done"] == saved["progress"]["total"]
    assert saved["recent"][-1]["template"] == "A"
    nxt = c.get("/api/training", params={"day": (D + timedelta(days=1)).isoformat()}).json()
    assert nxt["template"] == "B"
    # 手动换成恢复训练、改时间
    r = c.put(f"/api/training/{day}", json={"template": "R", "budget": 45, "checked": []})
    assert r.json()["template"] == "R" and r.json()["budget"] == 45
    assert c.put(f"/api/training/{day}", json={"template": "X"}).status_code == 422
