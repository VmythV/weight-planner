from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthSettings
from app.db import Store
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    # 明确关闭验证，不受本机 .env 影响；验证相关的测试见 test_auth.py
    return TestClient(create_app(Store(tmp_path / "test.db"), AuthSettings()))


def profile_body(**overrides) -> dict:
    today = date.today()
    body = {
        "sex": "male",
        "age": 30,
        "height_cm": 175,
        "activity": "light",
        "target_weight": 75,
        "plan_start": (today - timedelta(days=10)).isoformat(),
        "target_date": (today + timedelta(days=80)).isoformat(),
        "exercise_kcal": 0,
    }
    body.update(overrides)
    return body


def test_index_and_empty_dashboard(client):
    assert client.get("/").status_code == 200
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/api/dashboard").json() == {"ready": False, "reason": "no_profile"}


def test_profile_validation(client):
    bad = profile_body(target_date=date.today().isoformat(), plan_start=date.today().isoformat())
    assert client.put("/api/profile", json=bad).status_code == 422
    assert client.put("/api/profile", json=profile_body(sex="other")).status_code == 422


def test_full_flow(client):
    assert client.put("/api/profile", json=profile_body()).status_code == 200
    assert client.get("/api/dashboard").json()["reason"] == "no_data"
    for i in range(10, -1, -1):
        at = datetime.combine(date.today() - timedelta(days=i), datetime.min.time()).replace(hour=7)
        res = client.post("/api/weighins", json={"weight_kg": 85 - 0.1 * (10 - i), "measured_at": at.isoformat()})
        assert res.status_code == 201
    dash = client.get("/api/dashboard").json()
    assert dash["ready"]
    assert dash["card"]["latest_weight"] == 84.0
    assert len(client.get("/api/weighins?limit=5").json()) == 5


def test_auto_source_merges_packets_but_manual_does_not(client):
    now = datetime.now()
    client.post("/api/weighins", json={"weight_kg": 80.1, "measured_at": now.isoformat(), "source": "esp32"})
    client.post(
        "/api/weighins",
        json={
            "weight_kg": 80.2,
            "measured_at": (now + timedelta(seconds=20)).isoformat(),
            "impedance_high": 450.5,
            "source": "esp32",
        },
    )
    rows = client.get("/api/weighins").json()
    assert len(rows) == 1
    assert rows[0]["weight_kg"] == 80.2 and rows[0]["impedance_high"] == 450.5

    client.post("/api/weighins", json={"weight_kg": 80.0})
    client.post("/api/weighins", json={"weight_kg": 80.0})
    assert len(client.get("/api/weighins").json()) == 3


def test_import_and_delete(client):
    csv = "日期,体重\n2026-09-01,86.2\n2026-9-2 07:30,85.9\n2026-09-03\t85.7\n2026-02-30,85\nabc"
    res = client.post("/api/weighins/import", json={"csv": csv}).json()
    assert res == {"imported": 3, "skipped_lines": [1, 5, 6]}
    rows = client.get("/api/weighins").json()
    assert rows[1]["measured_at"].startswith("2026-09-02T07:30")
    assert rows[0]["measured_at"].startswith("2026-09-03T08:00")
    assert client.delete(f"/api/weighins/{rows[0]['id']}").status_code == 204
    assert client.delete(f"/api/weighins/{rows[0]['id']}").status_code == 404


def test_update_weighin(client):
    row = client.post("/api/weighins", json={"weight_kg": 80.0, "measured_at": "2026-09-01T07:30:00"}).json()
    res = client.patch(f"/api/weighins/{row['id']}", json={"weight_kg": 79.55})
    assert res.status_code == 200
    assert res.json()["weight_kg"] == 79.55
    assert res.json()["measured_at"].startswith("2026-09-01T07:30")

    res = client.patch(f"/api/weighins/{row['id']}", json={"measured_at": "2026-09-02T06:45:00"})
    assert res.json()["measured_at"].startswith("2026-09-02T06:45")
    assert res.json()["weight_kg"] == 79.55

    assert client.patch(f"/api/weighins/{row['id']}", json={"weight_kg": 5}).status_code == 422
    assert client.patch("/api/weighins/9999", json={"weight_kg": 80}).status_code == 404


def test_body_and_report(client):
    assert client.get("/api/report").json() == {"ready": False}
    client.put("/api/profile", json=profile_body())
    for i in range(10, -1, -1):
        at = datetime.combine(date.today() - timedelta(days=i), datetime.min.time()).replace(hour=7)
        client.post("/api/weighins", json={"weight_kg": 85 - 0.1 * (10 - i), "measured_at": at.isoformat()})
    later = datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
    client.post(
        "/api/weighins",
        json={"weight_kg": 84.1, "measured_at": later.isoformat(), "impedance_low": 480, "source": "esp32"},
    )

    report = client.get("/api/report").json()
    assert report["day"] == date.today().isoformat()
    assert report["weight"] == 84.0  # 当天最早一次
    assert report["change_vs_prev"] == pytest.approx(-0.1)
    assert report["next_day"] is None and report["prev_day"] is not None
    assert report["bia"]["composition"]["method"] == "bia"
    assert len(report["weighins"]) == 2

    first = client.get(f"/api/report?day={report['days'][0]}").json()
    assert first["prev_day"] is None and first["bia"] is None
    assert client.get("/api/report?day=2000-01-01").status_code == 404

    data = client.get("/api/body").json()
    assert data["ready"] and len(data["series"]["dates"]) == 11
    assert data["series"]["fat_bia"][-1] is not None and data["series"]["fat_bia"][0] is None
    assert data["latest_bia"]["day"] == date.today().isoformat()

    action = client.get("/api/dashboard").json()["action"]
    assert action["applicable"] and action["keep_date"]["intake"] >= 1500
