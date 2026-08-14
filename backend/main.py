"""
黑鑽選股系統 — 後端 API (FastAPI)
====================================
提供：
  - 設定 / 候選清單 / 持股追蹤 的讀寫 API（取代前端 window.storage）
  - 背景執行 engine.py 篩選（輪詢進度），或直接匯入已產生的 candidates.json
  - 打包後同時 serve 前端靜態檔（frontend/dist）
"""

import os
import threading
import time
import uuid
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import storage
from engine import ScreenerConfig, run_screener, fetch_nasdaq_universe

app = FastAPI(title="黑鑽選股系統 API")

# 若前端與後端分開部署（不同網域），需要開放 CORS；同源部署則此設定無害。
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Settings(BaseModel):
    totalCapital: float = 500000
    riskPct: float = 1


class Holding(BaseModel):
    _id: Optional[str] = None
    symbol: str = ""
    name: str = ""
    entryPrice: float = 0
    stopPrice: float = 0
    shares: int = 0
    entryDate: Optional[str] = None
    stage: str = "watching"
    note: str = ""


class ScreenRequest(BaseModel):
    source: str = "symbols"          # "symbols" | "nasdaq"
    symbols: Optional[List[str]] = None
    market_cap_min: float = ScreenerConfig.market_cap_min
    market_cap_max: float = ScreenerConfig.market_cap_max
    ps_max: float = ScreenerConfig.ps_ratio_max
    range_min: float = ScreenerConfig.range_position_min
    volume_min: float = ScreenerConfig.avg_volume_min


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return storage.load("settings")


@app.put("/api/settings")
def put_settings(settings: Settings):
    storage.save("settings", settings.dict())
    return settings.dict()


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
@app.get("/api/candidates")
def get_candidates():
    return storage.load("candidates")


@app.post("/api/candidates/import")
def import_candidates(payload: dict = Body(...)):
    """接受 engine.py 產出的完整 JSON（{generated_at, candidates: [...]}），或直接一個陣列。"""
    if isinstance(payload, list):
        data = {"generated_at": None, "candidates": payload}
    else:
        data = payload
    if "candidates" not in data or not isinstance(data["candidates"], list):
        raise HTTPException(400, "找不到 candidates 陣列")
    storage.save("candidates", data)
    return data


@app.delete("/api/candidates")
def clear_candidates():
    empty = {"generated_at": None, "candidates": []}
    storage.save("candidates", empty)
    return empty


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
@app.get("/api/holdings")
def get_holdings():
    return storage.load("holdings")


@app.post("/api/holdings")
def add_holding(holding: dict = Body(...)):
    holdings = storage.load("holdings") or []
    holding["_id"] = holding.get("_id") or uuid.uuid4().hex[:8]
    holdings.append(holding)
    storage.save("holdings", holdings)
    return holding


@app.put("/api/holdings/{holding_id}")
def update_holding(holding_id: str, patch: dict = Body(...)):
    holdings = storage.load("holdings") or []
    for h in holdings:
        if h.get("_id") == holding_id:
            h.update(patch)
            storage.save("holdings", holdings)
            return h
    raise HTTPException(404, "找不到該筆持股")


@app.delete("/api/holdings/{holding_id}")
def delete_holding(holding_id: str):
    holdings = storage.load("holdings") or []
    new_holdings = [h for h in holdings if h.get("_id") != holding_id]
    storage.save("holdings", new_holdings)
    return {"deleted": holding_id}


# ---------------------------------------------------------------------------
# 背景篩選 Job
# ---------------------------------------------------------------------------
_job_lock = threading.Lock()
_job_state = {
    "running": False,
    "processed": 0,
    "total": 0,
    "current_symbol": None,
    "passed_so_far": 0,
    "error": None,
    "finished_at": None,
}


def _run_job(symbols, cfg: ScreenerConfig):
    global _job_state
    try:
        def on_progress(i, total, symbol, passed_so_far):
            with _job_lock:
                _job_state.update(
                    processed=i, total=total, current_symbol=symbol, passed_so_far=passed_so_far
                )

        result = run_screener(symbols, cfg, verbose=False, progress_callback=on_progress)
        storage.save("candidates", result)
        with _job_lock:
            _job_state["error"] = None
    except Exception as e:
        with _job_lock:
            _job_state["error"] = str(e)
    finally:
        with _job_lock:
            _job_state["running"] = False
            _job_state["finished_at"] = time.time()


@app.post("/api/screen/run")
def start_screen(req: ScreenRequest):
    with _job_lock:
        if _job_state["running"]:
            raise HTTPException(409, "已有篩選工作正在執行中，請稍候")

    if req.source == "nasdaq":
        symbols = fetch_nasdaq_universe()
    elif req.symbols:
        symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    else:
        raise HTTPException(400, "請提供 symbols，或將 source 設為 'nasdaq'")

    cfg = ScreenerConfig(
        market_cap_min=req.market_cap_min,
        market_cap_max=req.market_cap_max,
        ps_ratio_max=req.ps_max,
        range_position_min=req.range_min,
        avg_volume_min=req.volume_min,
    )

    with _job_lock:
        _job_state.update(
            running=True, processed=0, total=len(symbols), current_symbol=None,
            passed_so_far=0, error=None, finished_at=None,
        )

    thread = threading.Thread(target=_run_job, args=(symbols, cfg), daemon=True)
    thread.start()
    return {"started": True, "universe_size": len(symbols)}


@app.get("/api/screen/status")
def screen_status():
    with _job_lock:
        return dict(_job_state)


# ---------------------------------------------------------------------------
# 前端靜態檔案 (frontend/dist)，需先在 frontend/ 執行 `npm run build`
# ---------------------------------------------------------------------------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa_catch_all(full_path: str):
        index_path = os.path.join(STATIC_DIR, "index.html")
        return FileResponse(index_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
