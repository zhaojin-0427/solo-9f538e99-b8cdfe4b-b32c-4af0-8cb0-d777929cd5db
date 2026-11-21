"""公交接续等待复盘服务。

- POST /days/{day}/plan     按运营日一次导入计划表（班次 + 接续，可在 /docs 操作）
- POST /days/{day}/actuals  成批补录 / 更正 / 撤销实发（actual 为 null 即撤销）
- GET  /days/{day}/trips/{trip_id}  查询单班：实发 + 接续等待区间
- GET  /days/{day}/actuals  列出当日全部实发（含已知等待）
- GET  /days / GET /days/{day}      运营日概览
- DELETE /days/{day}                删除运营日（用于重导计划）
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import connect, horizon, init_db, read_txn, write_txn
from .logic import (
    BatchConflict,
    BatchError,
    DayGraph,
    PlanError,
    apply_actuals,
    import_plan,
    wait_interval,
)

DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


# ---------------------------------------------------------------- 请求/响应模型

class TripIn(BaseModel):
    trip_id: str = Field(min_length=1, max_length=64, description="班次号")
    planned: int = Field(ge=0, description="计划发车分钟（运营日零点起算）")


class ConnectionIn(BaseModel):
    pred: str = Field(min_length=1, max_length=64, description="前序班次")
    succ: str = Field(min_length=1, max_length=64, description="本班")
    min_interval: int = Field(ge=0, description="最短接续间隔（分钟）")


class PlanIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "trips": [
                        {"trip_id": "A101", "planned": 480},
                        {"trip_id": "A102", "planned": 510},
                        {"trip_id": "B201", "planned": 500},
                    ],
                    "connections": [
                        {"pred": "A101", "succ": "B201", "min_interval": 15}
                    ],
                }
            ]
        }
    )

    trips: list[TripIn] = Field(min_length=1, description="各班计划发车分钟")
    connections: list[ConnectionIn] = Field(
        default_factory=list,
        description="必须满足的接续（每班至多 4 条前序，不得成环）",
    )


class ActualUpdate(BaseModel):
    trip_id: str = Field(min_length=1, max_length=64)
    actual: int | None = Field(
        default=None, ge=0, description="实发分钟；null 表示撤销该班实发"
    )


class ActualBatchIn(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "updates": [
                        {"trip_id": "A101", "actual": 487},
                        {"trip_id": "B201", "actual": 509},
                        {"trip_id": "A102", "actual": None},
                    ]
                }
            ]
        }
    )

    updates: list[ActualUpdate] = Field(description="成批补录/更正/撤销")


class WaitOut(BaseModel):
    known_wait: int = Field(description="按已知实发计算的接续等待（分钟）")
    min_wait: int = Field(description="所有合法补全中能达到的最小接续等待")
    max_wait: int = Field(description="所有合法补全中能达到的最大接续等待")


class TripOut(BaseModel):
    day: str
    trip_id: str
    planned: int
    actual: int | None
    wait: WaitOut | None = Field(description="仅当有实发时给出")


# ---------------------------------------------------------------- 应用与依赖

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="公交接续等待复盘",
    version="1.0.0",
    description=(
        "按运营日导入计划表，成批补录/更正/撤销实发，"
        "查询各班实发与接续等待区间（最少拖了多久、最多能解释几分钟）。"
    ),
    lifespan=lifespan,
)


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(sqlite3.OperationalError)
def sqlite_busy_handler(_request, exc):
    return JSONResponse(
        status_code=503,
        content={"detail": f"数据库暂时不可用，请重试：{exc}"},
    )


def _day_exists(conn, day: str) -> bool:
    return conn.execute("SELECT 1 FROM days WHERE day = ?", (day,)).fetchone() is not None


# ---------------------------------------------------------------- 接口

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.post("/days/{day}/plan", status_code=201, tags=["计划"])
def post_plan(
    body: PlanIn,
    day: str = Path(pattern=DAY_PATTERN, description="运营日 YYYY-MM-DD"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """按运营日一次导入计划表（校验：≤4 前序、无环、分钟不越界）。"""
    try:
        result = import_plan(conn, day, body.trips, body.connections, horizon())
    except PlanError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"message": exc.message, **exc.details}
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail={"message": "该运营日已有计划"})
    return {"day": day, **result}


@app.delete("/days/{day}", tags=["计划"])
def delete_day(
    day: str = Path(pattern=DAY_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """删除运营日及其全部计划与实发（用于重新导入）。"""
    with write_txn(conn):
        cur = conn.execute("DELETE FROM days WHERE day = ?", (day,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="运营日不存在")
    return {"deleted": day}


@app.post(
    "/days/{day}/actuals",
    tags=["实发"],
    responses={409: {"description": "批次与已知实发矛盾，已整体回滚"}},
)
def post_actuals(
    body: ActualBatchIn,
    day: str = Path(pattern=DAY_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """成批补录/更正/撤销实发。整批合看有矛盾则指出冲突时刻与接续链，且不写入任何更改。"""
    if not _day_exists(conn, day):
        raise HTTPException(status_code=404, detail="运营日不存在，请先导入计划")
    try:
        applied = apply_actuals(conn, day, body.updates, horizon())
    except BatchError as exc:
        raise HTTPException(
            status_code=400, detail={"message": exc.message, **exc.details}
        )
    except BatchConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "批次与已知实发矛盾，未写入任何更改",
                "total_conflicts": exc.total,
                "conflicts": exc.conflicts,
            },
        )
    return {"day": day, "applied": applied}


@app.get("/days/{day}/trips/{trip_id}", response_model=TripOut, tags=["查询"])
def get_trip(
    day: str = Path(pattern=DAY_PATTERN),
    trip_id: str = Path(description="班次号"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """查询单班：有实发时同时给出实发与接续等待区间（同一事务快照）。"""
    with read_txn(conn):
        graph = DayGraph(conn, day)
        row = graph.trip(trip_id)
        if row is None:
            raise HTTPException(status_code=404, detail="班次或运营日不存在")
        wait = (
            wait_interval(graph, trip_id, horizon())
            if row["actual"] is not None
            else None
        )
    return TripOut(
        day=day,
        trip_id=trip_id,
        planned=row["planned"],
        actual=row["actual"],
        wait=wait,
    )


@app.get("/days/{day}/actuals", tags=["查询"])
def list_actuals(
    day: str = Path(pattern=DAY_PATTERN),
    limit: int = Query(1000, ge=1, le=50000),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """列出当日全部实发（含按已知实发计算的接续等待）。"""
    with read_txn(conn):
        if not _day_exists(conn, day):
            raise HTTPException(status_code=404, detail="运营日不存在")
        rows = conn.execute(
            """
            SELECT t.trip_id  AS trip_id,
                   t.planned  AS planned,
                   t.actual   AS actual,
                   COALESCE(MAX(p.actual + c.min_interval - t.planned), 0) AS over
            FROM trips t
            LEFT JOIN connections c ON c.day = t.day AND c.succ = t.trip_id
            LEFT JOIN trips p
                   ON p.day = c.day AND p.trip_id = c.pred AND p.actual IS NOT NULL
            WHERE t.day = ? AND t.actual IS NOT NULL
            GROUP BY t.trip_id
            ORDER BY t.actual, t.trip_id
            LIMIT ? OFFSET ?
            """,
            (day, limit, offset),
        ).fetchall()
    items = [
        {
            "trip_id": r["trip_id"],
            "planned": r["planned"],
            "actual": r["actual"],
            "known_wait": max(0, r["over"]),
        }
        for r in rows
    ]
    return {"day": day, "count": len(items), "items": items}


@app.get("/days", tags=["查询"])
def list_days(conn: sqlite3.Connection = Depends(get_conn)):
    """全部运营日概览。"""
    with read_txn(conn):
        rows = conn.execute(
            """
            SELECT d.day           AS day,
                   COUNT(t.trip_id) AS trips,
                   COUNT(t.actual)  AS with_actual
            FROM days d
            LEFT JOIN trips t ON t.day = d.day
            GROUP BY d.day
            ORDER BY d.day
            """
        ).fetchall()
    return {"days": [dict(r) for r in rows]}


@app.get("/days/{day}", tags=["查询"])
def day_info(
    day: str = Path(pattern=DAY_PATTERN),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """单运营日概览。"""
    with read_txn(conn):
        row = conn.execute(
            """
            SELECT d.day            AS day,
                   COUNT(t.trip_id)  AS trips,
                   COUNT(t.actual)   AS with_actual,
                   (SELECT COUNT(*) FROM connections c WHERE c.day = d.day) AS connections
            FROM days d
            LEFT JOIN trips t ON t.day = d.day
            WHERE d.day = ?
            GROUP BY d.day
            """,
            (day,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="运营日不存在")
    return dict(row)
