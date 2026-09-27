import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post, put
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel

from domain import fmt_hhmm, in_ban_window, parse_hhmm

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
CREATE TABLE IF NOT EXISTS ban_window (
    id integer PRIMARY KEY,
    start_minutes integer NOT NULL,
    end_minutes integer NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS ban_traces (
    id serial PRIMARY KEY,
    username text NOT NULL,
    lamp text NOT NULL,
    nominal_nm double precision NOT NULL,
    measured_nm double precision NOT NULL,
    attempted_at timestamptz NOT NULL,
    window_start text NOT NULL,
    window_end text NOT NULL
);
"""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float


class BanWindowIn(BaseModel):
    start: str
    end: str


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


@get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "spectrum-wavelength-desk"}


def load_ban_window(conn) -> dict | None:
    return conn.execute(
        "SELECT start_minutes, end_minutes, updated_by, updated_at FROM ban_window WHERE id = 1"
    ).fetchone()


def ban_status(win: dict | None) -> tuple[bool, str]:
    """按服务器本地时刻判定此刻是否落在每日禁写闭区间内。"""
    now = datetime.now().astimezone()
    server_time = now.strftime("%H:%M:%S")
    if not win:
        return False, server_time
    now_minutes = now.hour * 60 + now.minute
    return in_ban_window(now_minutes, win["start_minutes"], win["end_minutes"]), server_time


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
        win = load_ban_window(conn)
        banned, server_time = ban_status(win)
        if banned:
            ws, we = fmt_hhmm(win["start_minutes"]), fmt_hhmm(win["end_minutes"])
            conn.execute(
                """
                INSERT INTO ban_traces(username, lamp, nominal_nm, measured_nm, attempted_at, window_start, window_end)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    user["username"],
                    data.lamp.strip(),
                    data.nominal_nm,
                    data.measured_nm,
                    datetime.now(timezone.utc),
                    ws,
                    we,
                ),
            )
            conn.commit()
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail=f"每日禁写窗 {ws}–{we} 内一律拒收（服务器时间 {server_time}），禁写窗外才放行",
            )
        row = conn.execute(
            """
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
            VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
            """,
            (data.lamp.strip(), data.nominal_nm, data.measured_nm, user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "status": "pending"}


@get("/api/ban-window")
async def get_ban_window(request: Request) -> dict:
    user_from_request(request)
    with connect() as conn:
        win = load_ban_window(conn)
    banned, server_time = ban_status(win)
    return {
        "configured": win is not None,
        "start": fmt_hhmm(win["start_minutes"]) if win else None,
        "end": fmt_hhmm(win["end_minutes"]) if win else None,
        "banned_now": banned,
        "server_time": server_time,
        "updated_by": win["updated_by"] if win else None,
        "updated_at": win["updated_at"].isoformat() if win else None,
    }


@put("/api/ban-window")
async def put_ban_window(request: Request, data: BanWindowIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可设置禁写区间")
    try:
        start_minutes = parse_hhmm(data.start)
        end_minutes = parse_hhmm(data.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ban_window(id, start_minutes, end_minutes, updated_by, updated_at)
            VALUES (1, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                start_minutes = EXCLUDED.start_minutes,
                end_minutes = EXCLUDED.end_minutes,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at
            """,
            (start_minutes, end_minutes, user["username"], datetime.now(timezone.utc)),
        )
        conn.commit()
    return {"configured": True, "start": fmt_hhmm(start_minutes), "end": fmt_hhmm(end_minutes)}


@get("/api/ban-traces")
async def list_ban_traces(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, username, lamp, nominal_nm, measured_nm, attempted_at, window_start, window_end
            FROM ban_traces ORDER BY id DESC LIMIT 200
            """
        ).fetchall()
    for r in rows:
        r["attempted_at"] = r["attempted_at"].isoformat()
    return list(rows)


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
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
        get_ban_window,
        put_ban_window,
        list_ban_traces,
    ],
    on_startup=[on_startup],
)
