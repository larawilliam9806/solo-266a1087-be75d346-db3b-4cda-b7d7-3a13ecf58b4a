"""FastAPI 入口：提交新旧静态路由表与保通要求，返回批数最少的换表计划。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .models import MAX_BATCH_SIZE, MAX_DEVICES, PlanRequest
from .planner import PlanError, solve

app = FastAPI(
    title="园区路由器静态路由整表切换排程",
    description=(
        "根据新旧 IPv4 静态路由表与保通要求（入口路由器 / 目的网段 / 指定出口），"
        "排出批数最少的整表切换计划。每批最多 3 台、最多 12 台设备，"
        "保证批内任意换表顺序下，网段内每个地址都按当时最长前缀匹配到达指定出口；"
        "校验基于路由等价类而非逐地址穷举；无解时给出可复现的断路/环路证据。"
    ),
    version="1.0.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/constraints")
def constraints() -> dict:
    return {
        "max_devices": MAX_DEVICES,
        "max_batch_size": MAX_BATCH_SIZE,
        "switch_granularity": "per-device whole-table atomic switch",
        "verification": "longest-prefix-match over routing equivalence classes (no per-address enumeration)",
    }


@app.post("/plan")
def plan(req: PlanRequest) -> JSONResponse:
    try:
        result = solve(req)
    except PlanError as exc:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": str(exc)},
            media_type="application/json; charset=utf-8",
        )
    return JSONResponse(content=result, media_type="application/json; charset=utf-8")
