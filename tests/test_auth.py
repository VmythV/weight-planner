import pytest
from fastapi.testclient import TestClient

from app.auth import AuthSettings, LoginThrottle
from app.config import read_env_file
from app.db import Store
from app.main import create_app

SETTINGS = AuthSettings(password="s3cret", api_tokens={"tok-esp32": "esp32", "tok-ha": "homeassistant"})


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Store(tmp_path / "test.db"), SETTINGS))


def test_from_config_parses_tokens():
    s = AuthSettings.from_config({"WP_PASSWORD": "pw", "WP_API_TOKENS": " esp32:aaa , bbb ,", "WP_SESSION_DAYS": "7"})
    assert s.enabled
    assert s.api_tokens == {"aaa": "esp32", "bbb": "token2"}
    assert s.session_days == 7
    assert not AuthSettings.from_config({}).enabled


def test_read_env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# 注释\nWP_PASSWORD="密 码"\nexport WP_DB=/tmp/x.db\nbad line\n', encoding="utf-8")
    assert read_env_file(f) == {"WP_PASSWORD": "密 码", "WP_DB": "/tmp/x.db"}
    assert read_env_file(tmp_path / "missing") == {}


def test_session_signature_and_expiry():
    value = SETTINGS.issue_session(now=1000)
    assert SETTINGS.check_session(value, now=1001)
    assert not SETTINGS.check_session(value, now=1000 + 31 * 86400)
    assert not SETTINGS.check_session(value.replace(".", ".0"), now=1001)
    other = AuthSettings(password="changed")
    assert not other.check_session(value, now=1001)  # 改密码后旧登录失效


def test_unauthenticated_requests(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    for path in ("/api/dashboard", "/api/weighins", "/openapi.json"):
        assert client.get(path).status_code == 401
    assert client.post("/api/weighins", json={"weight_kg": 80}).status_code == 401


def test_login_logout(client):
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 204
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.get("/api/session").json() == {"auth_enabled": True, "client": "web"}
    client.post("/api/logout")
    assert client.get("/api/dashboard").status_code == 401


def test_api_tokens(client):
    r = client.post("/api/weighins", json={"weight_kg": 80, "source": "esp32"}, headers={"X-API-Key": "tok-esp32"})
    assert r.status_code == 201
    r = client.get("/api/weighins", headers={"Authorization": "Bearer tok-ha"})
    assert r.status_code == 200 and len(r.json()) == 1
    assert client.get("/api/session", headers={"Authorization": "Bearer tok-ha"}).json()["client"] == "homeassistant"
    assert client.get("/api/weighins", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_login_throttle(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "x"}).status_code == 401
    r = client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_throttle_window():
    t = LoginThrottle(limit=2, window=60)
    t.fail("ip", now=0)
    t.fail("ip", now=1)
    assert t.retry_after("ip", now=2) == 58
    assert t.retry_after("ip", now=61) == 0


def test_disabled_auth_is_open(tmp_path):
    c = TestClient(create_app(Store(tmp_path / "t.db"), AuthSettings()))
    assert c.get("/api/dashboard").status_code == 200
    assert c.get("/login", follow_redirects=False).status_code == 303
    assert c.get("/api/session").json() == {"auth_enabled": False, "client": None}
