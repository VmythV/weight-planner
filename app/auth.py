"""访问控制：网页用一个密码登录（签名 Cookie），其他系统和 ESP32 用 API 令牌。

配置项（见 app/config.py）：
- WP_PASSWORD：网页登录密码。不设置时完全不做验证，只适合本机开发。
- WP_API_TOKENS：API 令牌，逗号分隔，每个可以写成「名称:令牌」，例如 esp32:3f9a…,homeassistant:81bc…
  请求时放在 Authorization: Bearer <令牌> 或 X-API-Key: <令牌> 头里。
- WP_SESSION_DAYS：登录有效天数，默认 30。
- WP_SECRET_KEY：Cookie 签名密钥，可选。不设置时由密码派生，改密码后所有已登录的设备都要重新登录。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

COOKIE_NAME = "wp_session"
# 同一 IP 在 FAIL_WINDOW 秒内输错 FAIL_LIMIT 次后，暂停登录到窗口结束
FAIL_LIMIT = 5
FAIL_WINDOW = 15 * 60


@dataclass(frozen=True)
class AuthSettings:
    password: str | None = None
    api_tokens: dict[str, str] = field(default_factory=dict)  # 令牌 -> 名称
    session_days: int = 30
    secret_key: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    @classmethod
    def from_config(cls, config: dict[str, str]) -> AuthSettings:
        tokens: dict[str, str] = {}
        for i, item in enumerate(filter(None, (s.strip() for s in config.get("WP_API_TOKENS", "").split(","))), 1):
            name, sep, token = item.rpartition(":")
            if not sep:
                name = f"token{i}"
            tokens[token.strip()] = name.strip() or f"token{i}"
        return cls(
            password=config.get("WP_PASSWORD") or None,
            api_tokens=tokens,
            session_days=int(config.get("WP_SESSION_DAYS") or 30),
            secret_key=config.get("WP_SECRET_KEY") or None,
        )

    def _key(self) -> bytes:
        secret = self.secret_key or f"weight-planner-session\0{self.password}"
        return hashlib.sha256(secret.encode()).digest()

    def check_password(self, password: str) -> bool:
        return bool(self.password) and hmac.compare_digest(password.encode(), self.password.encode())

    def token_name(self, token: str | None) -> str | None:
        if not token:
            return None
        for known, name in self.api_tokens.items():
            if hmac.compare_digest(token.encode(), known.encode()):
                return name
        return None

    def issue_session(self, now: float | None = None) -> str:
        expires = int((time.time() if now is None else now) + self.session_days * 86400)
        sig = hmac.new(self._key(), str(expires).encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{sig}"

    def check_session(self, value: str | None, now: float | None = None) -> bool:
        if not value or "." not in value:
            return False
        expires, sig = value.split(".", 1)
        if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
            return False
        expected = hmac.new(self._key(), expires.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)


def bearer_token(authorization: str | None, api_key: str | None) -> str | None:
    if api_key:
        return api_key.strip()
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            return token.strip()
    return None


class LoginThrottle:
    """按 IP 统计登录失败次数，防止暴力猜密码。"""

    def __init__(self, limit: int = FAIL_LIMIT, window: float = FAIL_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str, now: float) -> deque[float]:
        fails = self._fails[ip]
        while fails and fails[0] <= now - self.window:
            fails.popleft()
        return fails

    def retry_after(self, ip: str, now: float | None = None) -> int:
        """还要等多少秒才能再试，0 表示可以登录。"""
        now = time.time() if now is None else now
        fails = self._prune(ip, now)
        if len(fails) < self.limit:
            return 0
        return max(1, int(fails[0] + self.window - now))

    def fail(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
