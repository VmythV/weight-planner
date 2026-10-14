from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Store(tmp_path / "test.db")))


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
