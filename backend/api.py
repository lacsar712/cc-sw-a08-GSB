import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.status_codes import (
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel, field_validator

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    lamp text NOT NULL,
    nominal_nm double precision NOT NULL,
    measured_nm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS blackout_windows (
    id serial PRIMARY KEY,
    start_minute text NOT NULL,
    end_minute text NOT NULL,
    note text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS blackout_traces (
    id serial PRIMARY KEY,
    attempted_at timestamptz NOT NULL,
    server_minute text NOT NULL,
    lamp text NOT NULL DEFAULT '',
    nominal_nm double precision,
    measured_nm double precision,
    attempted_by text NOT NULL,
    window_id integer,
    start_minute text NOT NULL,
    end_minute text NOT NULL,
    reason text NOT NULL
);
"""

MINUTE_RE = __import__("re").compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float


class WindowIn(BaseModel):
    start_minute: str
    end_minute: str
    note: str = ""

    @field_validator("start_minute", "end_minute")
    @classmethod
    def _check_minute(cls, v: str) -> str:
        v = v.strip()
        if not MINUTE_RE.match(v):
            raise ValueError("时间格式须为 HH:MM（00:00-23:59）")
        return v


def user_from_request(request: Request) -> dict:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(auth[7:], SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


def require_writer(request: Request) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可修改禁写区间")
    return user


def server_now() -> datetime:
    return datetime.now(timezone.utc)


def minute_in_window(start_minute: str, end_minute: str, minute: str) -> bool:
    """每日闭区间命中；起止跨 00:00 时视为跨夜区间。端点均命中。"""
    if start_minute <= end_minute:
        return start_minute <= minute <= end_minute
    return minute >= start_minute or minute <= end_minute


def fetch_windows(conn) -> list[dict]:
    return list(
        conn.execute(
            "SELECT id, start_minute, end_minute, note, created_by, created_at, updated_at"
            " FROM blackout_windows ORDER BY id"
        ).fetchall()
    )


def find_hit_window(windows: list[dict], minute: str) -> dict | None:
    for w in windows:
        if minute_in_window(w["start_minute"], w["end_minute"], minute):
            return w
    return None


def blackout_deny_detail(minute: str, w: dict) -> str:
    return (
        f"此刻 {minute}（服务器时间）落在每日禁写闭区间 "
        f"{w['start_minute']}–{w['end_minute']} 内，提交一律拒收；"
        "请把禁写区间挪开或等禁写窗外再提交"
    )


@get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "spectrum-wavelength-desk"}


@post("/api/login")
async def login(data: LoginIn) -> dict:
    u = USERS.get(data.username)
    if not u or not pwd.verify(data.password, u["password_hash"]):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token = jwt.encode(
        {
            "sub": data.username,
            "role": u["role"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "role": u["role"], "username": data.username}


@get("/api/jobs")
async def list_jobs(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()
        return list(rows)


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(row)


@post("/api/jobs")
async def create_job(request: Request, data: JobIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可提交")
    with connect() as conn:
        now = server_now()
        minute = now.strftime("%H:%M")
        hit = find_hit_window(fetch_windows(conn), minute)
        if hit:
            conn.execute(
                """
                INSERT INTO blackout_traces(
                    attempted_at, server_minute, lamp, nominal_nm, measured_nm,
                    attempted_by, window_id, start_minute, end_minute, reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    now,
                    minute,
                    data.lamp.strip(),
                    data.nominal_nm,
                    data.measured_nm,
                    user["username"],
                    hit["id"],
                    hit["start_minute"],
                    hit["end_minute"],
                    blackout_deny_detail(minute, hit),
                ),
            )
            conn.commit()
            raise HTTPException(status_code=HTTP_409_CONFLICT, detail=blackout_deny_detail(minute, hit))
        row = conn.execute(
            """
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
            VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
            """,
            (data.lamp.strip(), data.nominal_nm, data.measured_nm, user["username"], now),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "status": "pending"}


@get("/api/blackout/status")
async def blackout_status(request: Request) -> dict:
    user = user_from_request(request)
    with connect() as conn:
        windows = fetch_windows(conn)
    now = server_now()
    minute = now.strftime("%H:%M")
    hit = find_hit_window(windows, minute)
    return {
        "server_time": now.isoformat(),
        "server_minute": minute,
        "blocked": hit is not None,
        "window": (
            {
                "id": hit["id"],
                "start_minute": hit["start_minute"],
                "end_minute": hit["end_minute"],
                "note": hit["note"],
            }
            if hit
            else None
        ),
        "role": user["role"],
    }


@get("/api/blackout/windows")
async def list_blackout_windows(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        return fetch_windows(conn)


@post("/api/blackout/windows")
async def create_blackout_window(request: Request, data: WindowIn) -> dict:
    user = require_writer(request)
    now = server_now()
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO blackout_windows(start_minute, end_minute, note, created_by, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (data.start_minute, data.end_minute, data.note.strip(), user["username"], now, now),
        ).fetchone()
        conn.commit()
        return {"id": row["id"]}


@post("/api/blackout/windows/{window_id:int}")
async def update_blackout_window(request: Request, window_id: int, data: WindowIn) -> dict:
    require_writer(request)
    now = server_now()
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE blackout_windows SET start_minute=%s, end_minute=%s, note=%s, updated_at=%s
            WHERE id=%s RETURNING id
            """,
            (data.start_minute, data.end_minute, data.note.strip(), now, window_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="禁写区间不存在")
        conn.commit()
        return {"id": row["id"]}


@post("/api/blackout/windows/{window_id:int}/delete")
async def delete_blackout_window(request: Request, window_id: int) -> dict:
    require_writer(request)
    with connect() as conn:
        row = conn.execute("DELETE FROM blackout_windows WHERE id=%s RETURNING id", (window_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="禁写区间不存在")
        conn.commit()
        return {"deleted": window_id}


@get("/api/blackout/traces")
async def list_blackout_traces(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT id, attempted_at, server_minute, lamp, nominal_nm, measured_nm,
                       attempted_by, window_id, start_minute, end_minute, reason
                FROM blackout_traces ORDER BY id DESC
                """
            ).fetchall()
        )


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = server_now()
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s)
                """,
                (now, now),
            )
        conn.commit()


app = Litestar(
    route_handlers=[
        health,
        login,
        list_jobs,
        get_job,
        create_job,
        blackout_status,
        list_blackout_windows,
        create_blackout_window,
        update_blackout_window,
        delete_blackout_window,
        list_blackout_traces,
    ],
    on_startup=[on_startup],
)
