"""减重计划 Web 服务。

启动：uv run uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import planner
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


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store(Path(os.environ.get("WP_DB", DEFAULT_DB)))
    app = FastAPI(title="减重计划")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        profile = _profile_from_dict(store.get_profile())
        weighins = [
            planner.Weighin(datetime.fromisoformat(w["measured_at"]), w["weight_kg"])
            for w in store.list_weighins()
        ]
        return planner.build_dashboard(profile, weighins, date.today())

    return app
