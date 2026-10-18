"""减重计划 Web 服务。

启动：uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import body, planner, training
from .auth import COOKIE_NAME, AuthSettings, LoginThrottle, bearer_token
from .config import load_config
from .db import Store

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "weight.db"
IMPORT_DEFAULT_TIME = time(8, 0)  # 只有日期的历史数据按早上 8 点记
NON_MERGING_SOURCES = {"manual", "import"}
# 2026-09-01,85.2 / 2026-09-01 07:30,85.2 / 2026-09-01	85.2
_IMPORT_LINE = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?\s*[,，\t ]\s*(\d+(?:\.\d+)?)\s*$"
)


class ProfileIn(BaseModel):
    sex: Literal["male", "female"]
    age: int = Field(ge=14, le=100)
    height_cm: float = Field(ge=100, le=250)
    activity: Literal["sedentary", "light", "moderate", "active"]
    target_weight: float = Field(ge=30, le=300)
    plan_start: date
    target_date: date
    exercise_kcal: float = Field(default=0, ge=0, le=2000)
    start_weight: float | None = Field(default=None, ge=30, le=300)

    @model_validator(mode="after")
    def _dates_in_order(self) -> ProfileIn:
        if self.target_date <= self.plan_start:
            raise ValueError("目标日期必须晚于计划开始日期")
        return self


class WeighinIn(BaseModel):
    weight_kg: float = Field(ge=20, le=300)
    measured_at: datetime | None = None
    impedance_low: float | None = Field(default=None, ge=0)
    impedance_high: float | None = Field(default=None, ge=0)
    heart_rate: int | None = Field(default=None, ge=20, le=250)
    source: str = Field(default="manual", min_length=1, max_length=20)


class WeighinPatch(BaseModel):
    weight_kg: float | None = Field(default=None, ge=20, le=300)
    measured_at: datetime | None = None


class WorkoutIn(BaseModel):
    template: Literal["A", "B", "C", "R"] | None = None
    budget: int | None = Field(default=None, ge=30, le=150)
    checked: list[str] = Field(default_factory=list, max_length=200)
    weights: dict[str, float] = Field(default_factory=dict, max_length=50)


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


class ImportIn(BaseModel):
    csv: str = Field(max_length=500_000)


def _to_local(dt: datetime | None) -> datetime:
    # 不带时区的时间按本机时区理解
    return (dt or datetime.now()).astimezone()


def _profile_from_dict(data: dict | None) -> planner.Profile | None:
    if data is None:
        return None
    return planner.Profile(
        sex=data["sex"],
        age=data["age"],
        height_cm=data["height_cm"],
        activity=data["activity"],
        target_weight=data["target_weight"],
        plan_start=date.fromisoformat(data["plan_start"]),
        target_date=date.fromisoformat(data["target_date"]),
        exercise_kcal=data.get("exercise_kcal") or 0.0,
        start_weight=data.get("start_weight"),
    )


def _person(p: planner.Profile) -> body.Person:
    return body.Person(sex=p.sex, age=p.age, height_cm=p.height_cm)


def _group_by_day(rows: list[dict]) -> dict[str, list[dict]]:
    """按日期分组，组内按时间升序（第一条就是当天计入计算的最早一次），日期升序。"""
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["measured_at"], r["id"])):
        out.setdefault(r["measured_at"][:10], []).append(r)
    return out


def _bia_row(day_rows: list[dict]) -> dict | None:
    """当天最后一条带 50 kHz 阻抗的称重（体重秤光脚测量）。"""
    return next((r for r in reversed(day_rows) if r.get("impedance_low")), None)


# 不需要登录就能访问的路径；其余页面和 /api、/docs 都要登录或带 API 令牌
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(store: Store | None = None, auth: AuthSettings | None = None) -> FastAPI:
    config = load_config()
    store = store or Store(Path(config.get("WP_DB", DEFAULT_DB)))
    auth = auth or AuthSettings.from_config(config)
    throttle = LoginThrottle()
    app = FastAPI(title="减重计划")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        path = request.url.path
        if not auth.enabled or path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        token = bearer_token(request.headers.get("authorization"), request.headers.get("x-api-key"))
        if token is not None:
            name = auth.token_name(token)
            if name is None:
                return JSONResponse({"detail": "API 令牌无效"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            request.state.client = name
            return await call_next(request)
        if auth.check_session(request.cookies.get(COOKIE_NAME)):
            request.state.client = "web"
            return await call_next(request)
        if path.startswith("/api/") or path in ("/docs", "/redoc", "/openapi.json"):
            return JSONResponse({"detail": "需要登录或 API 令牌"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def revalidate_pages(request: Request, call_next):
        # 页面和静态文件每次都向服务器确认是否更新（未修改时返回 304，开销很小），
        # 避免浏览器拿缓存里的旧 JS/CSS 配新 HTML
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if not auth.enabled or auth.check_session(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})

    @app.post("/api/login", status_code=204)
    def login(body: LoginIn, request: Request, response: Response) -> None:
        if not auth.enabled:
            return
        ip = _client_ip(request)
        wait = throttle.retry_after(ip)
        if wait:
            raise HTTPException(429, detail=f"密码错误次数过多，请 {wait // 60 + 1} 分钟后再试", headers={"Retry-After": str(wait)})
        if not auth.check_password(body.password):
            throttle.fail(ip)
            raise HTTPException(401, detail="密码不正确")
        throttle.reset(ip)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_session(),
            max_age=auth.session_days * 86400,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )

    @app.post("/api/logout", status_code=204)
    def logout(response: Response) -> None:
        response.delete_cookie(COOKIE_NAME)

    @app.get("/api/session")
    def session(request: Request) -> dict:
        """当前请求的身份：web（浏览器登录）、API 令牌的名称，或未启用验证时为 null。"""
        return {"auth_enabled": auth.enabled, "client": getattr(request.state, "client", None)}

    @app.get("/api/profile")
    def get_profile() -> dict | None:
        return store.get_profile()

    @app.put("/api/profile")
    def put_profile(body: ProfileIn) -> dict:
        data = body.model_dump(mode="json")
        store.save_profile(data)
        return data

    @app.get("/api/weighins")
    def list_weighins(limit: int = 60) -> list[dict]:
        return store.list_weighins(limit=min(max(limit, 1), 1000), newest_first=True)

    @app.post("/api/weighins", status_code=201)
    def add_weighin(body: WeighinIn) -> dict:
        return store.add_weighin(
            measured_at=_to_local(body.measured_at),
            weight_kg=body.weight_kg,
            impedance_low=body.impedance_low,
            impedance_high=body.impedance_high,
            heart_rate=body.heart_rate,
            source=body.source,
            merge=body.source not in NON_MERGING_SOURCES,
        )

    @app.patch("/api/weighins/{weighin_id}")
    def update_weighin(weighin_id: int, body: WeighinPatch) -> dict:
        row = store.update_weighin(
            weighin_id,
            measured_at=_to_local(body.measured_at) if body.measured_at else None,
            weight_kg=body.weight_kg,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return row

    @app.delete("/api/weighins/{weighin_id}", status_code=204)
    def delete_weighin(weighin_id: int) -> None:
        if not store.delete_weighin(weighin_id):
            raise HTTPException(status_code=404, detail="记录不存在")

    @app.post("/api/weighins/import")
    def import_weighins(body: ImportIn) -> dict:
        imported, skipped = 0, []
        for lineno, line in enumerate(body.csv.splitlines(), start=1):
            if not line.strip():
                continue
            m = _IMPORT_LINE.match(line)
            try:
                if not m:
                    raise ValueError
                day = date(int(m[1]), int(m[2]), int(m[3]))
                at = time(int(m[4]), int(m[5])) if m[4] else IMPORT_DEFAULT_TIME
                weight = float(m[6])
                if not 20 <= weight <= 300:
                    raise ValueError
            except ValueError:
                skipped.append(lineno)
                continue
            store.add_weighin(_to_local(datetime.combine(day, at)), weight, source="import")
            imported += 1
        return {"imported": imported, "skipped_lines": skipped}

    def _dashboard(rows: list[dict]) -> tuple[planner.Profile | None, dict]:
        profile = _profile_from_dict(store.get_profile())
        weighins = [planner.Weighin(datetime.fromisoformat(w["measured_at"]), w["weight_kg"]) for w in rows]
        return profile, planner.build_dashboard(profile, weighins, date.today())

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return _dashboard(store.list_weighins())[1]

    @app.get("/api/body")
    def body_series() -> dict:
        """按天的身体指标：BMI、按 BMI 估算的体脂率基于趋势体重，BIA 指标来自体重秤当天的测量。"""
        rows = store.list_weighins()
        profile, dash = _dashboard(rows)
        if profile is None or not dash["ready"]:
            return {"ready": False}
        person = _person(profile)
        by_day = _group_by_day(rows)
        trend = dict(zip(dash["series"]["dates"], dash["series"]["trend"]))
        out: dict[str, list] = {k: [] for k in ("dates", "weight", "trend", "bmi", "fat_est", "fat_bia", "muscle_pct", "water_pct")}
        for day, day_rows in by_day.items():
            t = trend.get(day)
            bia = _bia_row(day_rows)
            comp = body.composition(bia["weight_kg"], person, bia["impedance_low"]) if bia else None
            out["dates"].append(day)
            out["weight"].append(day_rows[0]["weight_kg"])
            out["trend"].append(t)
            out["bmi"].append(round(body.bmi(t, person), 2) if t else None)
            out["fat_est"].append(round(body.fat_pct_from_bmi(t, person), 2) if t else None)
            out["fat_bia"].append(comp["fat_pct"] if comp else None)
            out["muscle_pct"].append(comp["muscle_pct"] if comp else None)
            out["water_pct"].append(comp["water_pct"] if comp else None)
        latest_day = out["dates"][-1]
        latest_rows = by_day[latest_day]
        latest_bia = next((d for d in reversed(out["dates"]) if _bia_row(by_day[d])), None)
        bia_row = _bia_row(by_day[latest_bia]) if latest_bia else None
        return {
            "ready": True,
            "series": out,
            "latest": {
                "day": latest_day,
                "composition": body.composition(latest_rows[0]["weight_kg"], person),
                "trend_composition": body.composition(trend[latest_day], person) if trend.get(latest_day) else None,
            },
            "latest_bia": {
                "day": latest_bia,
                "measured_at": bia_row["measured_at"],
                "heart_rate": bia_row["heart_rate"],
                "impedance_low": bia_row["impedance_low"],
                "impedance_high": bia_row["impedance_high"],
                "composition": body.composition(bia_row["weight_kg"], person, bia_row["impedance_low"]),
            }
            if bia_row
            else None,
            "target": body.composition(profile.target_weight, person),
        }

    def _training_inputs() -> tuple[planner.Profile, float, dict[date, training.DayState]] | None:
        rows = store.list_weighins()
        profile, dash = _dashboard(rows)
        if profile is None or not rows:
            return None
        trend = [w for w in (dash.get("series") or {}).get("trend", []) if w is not None]
        weight = trend[-1] if trend else rows[-1]["weight_kg"]
        history = {date.fromisoformat(d): training.DayState.from_dict(v) for d, v in store.list_workouts().items()}
        return profile, weight, history

    def _training_plan(day: date, inputs) -> dict:
        profile, weight, history = inputs
        plan = training.build_plan(day, history, age=profile.age, weight=weight, height_cm=profile.height_cm)
        plan["ready"] = True
        plan["recent"] = training.recent_days(day, history)
        return plan

    @app.get("/api/training")
    def get_training(day: date | None = None) -> dict:
        """某一天（默认今天）的健身房训练清单。"""
        inputs = _training_inputs()
        if inputs is None:
            return {"ready": False}
        return _training_plan(day or date.today(), inputs)

    @app.put("/api/training/{day}")
    def put_training(day: date, body: WorkoutIn) -> dict:
        """保存当天的打勾、重量、手动选择的训练和可用时间，返回重新计算的清单。"""
        inputs = _training_inputs()
        if inputs is None:
            raise HTTPException(status_code=409, detail="请先填写计划设置并记录一次体重")
        profile, weight, history = inputs
        state = training.DayState(
            template=body.template,
            budget=body.budget,
            checked=body.checked,
            weights={k: v for k, v in body.weights.items() if 0 < v <= 500},
        )
        history[day] = state
        plan = training.build_plan(day, history, age=profile.age, weight=weight, height_cm=profile.height_cm)
        state.checked = plan["state"]["checked"]
        state.effective_template = plan["template"]
        state.completed = training.completed_exercises(plan, state.checked)
        state.total = plan["progress"]["total"]
        store.save_workout(day.isoformat(), state.to_dict())
        return _training_plan(day, (profile, weight, history))

    @app.get("/api/report")
    def day_report(day: date | None = None) -> dict:
        """某一天的体重报告。day 为空时取最近一次称重的日期。"""
        rows = store.list_weighins()
        profile, dash = _dashboard(rows)
        if profile is None or not dash["ready"]:
            return {"ready": False}
        by_day = _group_by_day(rows)
        days = list(by_day)
        key = (day.isoformat() if day else days[-1]) if days else None
        if key not in by_day:
            raise HTTPException(status_code=404, detail="这一天没有称重记录")
        person = _person(profile)
        s = dash["series"]
        idx = {d: i for i, d in enumerate(s["dates"])}
        i = idx.get(key)

        def at(name: str, d: str | None = key):
            j = idx.get(d) if d else None
            return s[name][j] if j is not None else None

        day_rows = by_day[key]
        counted = day_rows[0]
        pos = days.index(key)
        prev_day = days[pos - 1] if pos > 0 else None
        next_day = days[pos + 1] if pos + 1 < len(days) else None
        week_ago = (date.fromisoformat(key) - timedelta(days=7)).isoformat()
        trend_now = at("trend")
        trend_week = planner.value_on_or_before(
            [(date.fromisoformat(d), w) for d, w in zip(s["dates"], s["trend"]) if w is not None],
            date.fromisoformat(week_ago),
        )
        bia = _bia_row(day_rows)
        plan_w = at("plan")
        return {
            "ready": True,
            "day": key,
            "days": days,
            "prev_day": prev_day,
            "next_day": next_day,
            "weighins": day_rows,
            "counted": counted,
            "weight": counted["weight_kg"],
            "change_vs_prev": round(counted["weight_kg"] - by_day[prev_day][0]["weight_kg"], 2) if prev_day else None,
            "prev_weight": by_day[prev_day][0]["weight_kg"] if prev_day else None,
            "trend": trend_now,
            "trend_change_7d": round(trend_now - trend_week, 2) if trend_now is not None and trend_week else None,
            "plan": plan_w,
            "diff": round(trend_now - plan_w, 2) if trend_now is not None and plan_w is not None else None,
            "intake": at("intake"),
            "burn": at("burn"),
            "deficit": at("deficit"),
            "in_plan": i is not None and plan_w is not None,
            "composition": body.composition(counted["weight_kg"], person),
            "bia": {
                "measured_at": bia["measured_at"],
                "heart_rate": bia["heart_rate"],
                "impedance_low": bia["impedance_low"],
                "impedance_high": bia["impedance_high"],
                "composition": body.composition(bia["weight_kg"], person, bia["impedance_low"]),
            }
            if bia
            else None,
            "target_weight": profile.target_weight,
        }

    return app
