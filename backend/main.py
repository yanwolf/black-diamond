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
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import storage
from engine import ScreenerConfig, run_screener, fetch_nasdaq_universe

app = FastAPI(title="黑鑽選股系統 API")
scheduler = BackgroundScheduler(timezone="UTC")
SCHEDULE_JOB_ID = "full_market_scan"

# 個股快取有效時數：這段時間內已檢查過的股票，重新掃描時會直接沿用結果，不再打 Yahoo Finance
CACHE_FRESHNESS_HOURS = float(os.environ.get("CACHE_FRESHNESS_HOURS", 24))

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


class ScheduleConfig(BaseModel):
    enabled: bool = False
    interval_hours: float = 168   # 預設每週掃描一次


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
    "mode": None,               # "symbols" | "nasdaq"
    "processed": 0,
    "total": 0,
    "current_symbol": None,
    "passed_so_far": 0,
    "cache_hits": 0,
    "fresh_fetches": 0,
    "error": None,
    "finished_at": None,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _run_job(symbols, cfg: ScreenerConfig, mode: str):
    global _job_state
    cache = storage.load("symbol_cache") or {}
    try:
        def on_progress(i, total, symbol, passed_so_far, from_cache):
            with _job_lock:
                _job_state.update(
                    processed=i, total=total, current_symbol=symbol, passed_so_far=passed_so_far,
                    cache_hits=_job_state["cache_hits"] + (1 if from_cache else 0),
                    fresh_fetches=_job_state["fresh_fetches"] + (0 if from_cache else 1),
                )
            if i % 25 == 0:
                # 週期性把快取存檔，若中途被中斷（例如服務重啟），下次重跑不用從頭全部重查
                storage.save("symbol_cache", cache)

        result = run_screener(
            symbols, cfg, verbose=False, progress_callback=on_progress,
            cache=cache, freshness_hours=CACHE_FRESHNESS_HOURS,
        )
        storage.save("candidates", result)
        storage.save("symbol_cache", cache)
        with _job_lock:
            _job_state["error"] = None
        if mode == "nasdaq":
            sched = storage.load("schedule")
            sched["last_run_at"] = _now_iso()
            sched["last_result_count"] = result.get("candidate_count")
            storage.save("schedule", sched)
    except Exception as e:
        storage.save("symbol_cache", cache)  # 就算失敗也保留已查到的部分，避免全部浪費
        with _job_lock:
            _job_state["error"] = str(e)
    finally:
        with _job_lock:
            _job_state["running"] = False
            _job_state["finished_at"] = time.time()


def _start_job(symbols, cfg: ScreenerConfig, mode: str) -> bool:
    """回傳 True 表示成功啟動，False 表示已有工作在跑而略過。"""
    with _job_lock:
        if _job_state["running"]:
            return False
        _job_state.update(
            running=True, mode=mode, processed=0, total=len(symbols), current_symbol=None,
            passed_so_far=0, cache_hits=0, fresh_fetches=0, error=None, finished_at=None,
        )
    thread = threading.Thread(target=_run_job, args=(symbols, cfg, mode), daemon=True)
    thread.start()
    return True


@app.post("/api/screen/run")
def start_screen(req: ScreenRequest):
    if req.source == "nasdaq":
        symbols = fetch_nasdaq_universe()
        mode = "nasdaq"
    elif req.symbols:
        symbols = [s.strip().upper() for s in req.symbols if s.strip()]
        mode = "symbols"
    else:
        raise HTTPException(400, "請提供 symbols，或將 source 設為 'nasdaq'")

    cfg = ScreenerConfig(
        market_cap_min=req.market_cap_min,
        market_cap_max=req.market_cap_max,
        ps_ratio_max=req.ps_max,
        range_position_min=req.range_min,
        avg_volume_min=req.volume_min,
    )

    started = _start_job(symbols, cfg, mode)
    if not started:
        raise HTTPException(409, "已有篩選工作正在執行中，請稍候")
    return {"started": True, "universe_size": len(symbols)}


@app.get("/api/screen/status")
def screen_status():
    with _job_lock:
        return dict(_job_state)


# ---------------------------------------------------------------------------
# 全市場掃描 + 排程
# ---------------------------------------------------------------------------
def _run_full_market_scan_job():
    """供手動按鈕與排程器共用的進入點。用預設濾網參數跑全市場（NASDAQ+NYSE）。"""
    try:
        symbols = fetch_nasdaq_universe()
    except Exception as e:
        with _job_lock:
            _job_state["error"] = f"抓取上市清單失敗: {e}"
        return
    started = _start_job(symbols, ScreenerConfig(), "nasdaq")
    if not started:
        print("[scheduler] 略過本次排程：已有篩選工作正在執行中")


def _apply_schedule(cfg: dict):
    """依 schedule 設定重新註冊/移除 APScheduler 的定期任務。"""
    if scheduler.get_job(SCHEDULE_JOB_ID):
        scheduler.remove_job(SCHEDULE_JOB_ID)
    if cfg.get("enabled"):
        # 全市場掃描要對 Yahoo Finance 發出數千次請求，下限設為6小時避免掃太頻繁把來源打爆
        interval_hours = max(float(cfg.get("interval_hours", 168)), 6)
        cfg["interval_hours"] = interval_hours
        job = scheduler.add_job(
            _run_full_market_scan_job,
            trigger=IntervalTrigger(hours=interval_hours),
            id=SCHEDULE_JOB_ID,
            replace_existing=True,
        )
        cfg["next_run_at"] = job.next_run_time.isoformat() if job.next_run_time else None
    else:
        cfg["next_run_at"] = None
    storage.save("schedule", cfg)
    return cfg


@app.on_event("startup")
def on_startup():
    scheduler.start()
    cfg = storage.load("schedule")
    if cfg.get("enabled"):
        _apply_schedule(cfg)


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


@app.post("/api/scan/full-market/run")
def run_full_market_scan_now():
    try:
        symbols = fetch_nasdaq_universe()
    except Exception as e:
        raise HTTPException(502, f"抓取 NASDAQ/NYSE 上市清單失敗: {e}")
    started = _start_job(symbols, ScreenerConfig(), "nasdaq")
    if not started:
        raise HTTPException(409, "已有篩選工作正在執行中，請稍候")
    return {"started": True, "universe_size": len(symbols)}


@app.get("/api/scan/schedule")
def get_schedule():
    return storage.load("schedule")


@app.put("/api/scan/schedule")
def put_schedule(cfg: ScheduleConfig):
    saved = _apply_schedule(cfg.dict() | {
        "last_run_at": storage.load("schedule").get("last_run_at"),
        "last_result_count": storage.load("schedule").get("last_result_count"),
    })
    return saved


# ---------------------------------------------------------------------------
# 個股快取
# ---------------------------------------------------------------------------
@app.get("/api/cache/stats")
def cache_stats():
    cache = storage.load("symbol_cache") or {}
    return {"cached_symbols": len(cache), "freshness_hours": CACHE_FRESHNESS_HOURS}


@app.delete("/api/cache")
def clear_cache():
    storage.save("symbol_cache", {})
    return {"cleared": True}


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
