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
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
import yfinance as yf
import pandas as pd
import requests as http_requests

import storage
import momentum
from engine import ScreenerConfig, run_screener, fetch_nasdaq_universe, analyze_pattern

app = FastAPI(title="黑鑽選股系統 API")
scheduler = BackgroundScheduler(timezone="UTC")
SCHEDULE_JOB_ID = "full_market_scan"
MOMENTUM_JOB_ID = "momentum_daily_check"

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


class MomentumSettings(BaseModel):
    benchmark_symbol: str = "QQQ"
    trade_symbol: str = "QLD"
    initial_capital: float = 0
    check_day_of_month: int = 11
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    auto_schedule_enabled: bool = False


class MomentumImportRow(BaseModel):
    date: str    # "YYYY-MM-DD"
    price: float


class MomentumImportRequest(BaseModel):
    history: List[MomentumImportRow]
    position: str = "cash"   # "stock" | "cash"
    shares: float = 0
    cash: float = 0
    initial_capital: float = 0


class MomentumStateOverride(BaseModel):
    position: str          # "stock" | "cash"
    shares: float = 0
    cash: float = 0
    note: Optional[str] = None   # 順便記錄這次為什麼手動override，會附加到最新一筆歷史紀錄


class MomentumTelegramTestRequest(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None


class MomentumHistoryNote(BaseModel):
    note: str


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
    "phase": None,              # "screening" | "pattern"
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


def _annotate_pattern_scores(candidates: list):
    """對已通過6道濾網的候選股，額外抓歷史週線計算型態分數（輔助排序，非取代人工判斷）。"""
    total = len(candidates)
    with _job_lock:
        _job_state.update(phase="pattern", processed=0, total=total, current_symbol=None)
    for i, c in enumerate(candidates, 1):
        symbol = c.get("symbol")
        with _job_lock:
            _job_state.update(processed=i, current_symbol=symbol)
        try:
            payload = _get_history_payload(symbol, period="5y", interval="1wk")
            closes = [p["close"] for p in payload["points"] if p.get("close") is not None]
            c.update(analyze_pattern(closes))
        except Exception as e:
            c.update({
                "pattern_score": None,
                "base_range_pct": None,
                "base_trend_pct": None,
                "breakout_confirmed": None,
                "breakout_extension_pct": None,
                "pattern_note": f"型態分析失敗: {e}",
            })


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

        with _job_lock:
            _job_state.update(phase="screening")

        result = run_screener(
            symbols, cfg, verbose=False, progress_callback=on_progress,
            cache=cache, freshness_hours=CACHE_FRESHNESS_HOURS,
        )
        storage.save("symbol_cache", cache)

        # 篩選完成後，只對「通過6道濾網」的少數候選股額外算型態分數，不會對整個母體加重負擔
        try:
            _annotate_pattern_scores(result["candidates"])
        except Exception:
            pass  # 型態分析是輔助功能，就算整批失敗也不該讓篩選結果整個作廢

        storage.save("candidates", result)
        with _job_lock:
            _job_state["error"] = None
        if mode == "nasdaq":
            sched = storage.load("schedule")
            sched["last_run_at"] = _now_iso()
            sched["last_result_count"] = result.get("candidate_count")
            storage.save("schedule", sched)

            # 全市場掃描完成後通知（跟超速大盤共用同一組 Telegram 設定），
            # 讓使用者知道可以進來做人工複核，不用一直開著網頁等
            candidates = result.get("candidates", [])
            scored = sorted(
                (c for c in candidates if c.get("pattern_score") is not None),
                key=lambda c: c["pattern_score"],
                reverse=True,
            )[:5]
            if scored:
                top_lines = "\n".join(f"  {c['symbol']} ({c['pattern_score']}分)" for c in scored)
            elif candidates:
                top_lines = "  （尚未算出型態分數）"
            else:
                top_lines = "  （本次無候選股通過濾網）"
            send_telegram_message(
                f"[黑鑽選股] 全市場掃描完成：從 {result.get('universe_size')} 檔中篩選出 "
                f"{result.get('candidate_count')} 檔候選股。\n"
                f"型態分數最高幾檔：\n{top_lines}\n"
                f"請進來「選股清單」做人工複核。"
            )
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
            running=True, mode=mode, phase="screening", processed=0, total=len(symbols), current_symbol=None,
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
    # 超速大盤策略：每天固定時間跑一次（內部會自行判斷今天要不要真的動作）
    scheduler.add_job(
        momentum_daily_job,
        trigger=CronTrigger(hour=21, minute=30),  # UTC 21:30，約當美股收盤後
        id=MOMENTUM_JOB_ID,
        replace_existing=True,
    )


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
# 個股歷史線圖（供人工判斷5年線型態、以及型態分數計算共用）
# ---------------------------------------------------------------------------
_chart_cache_lock = threading.Lock()
_chart_cache = {}  # f"{symbol}:{period}:{interval}" -> (timestamp, payload)
CHART_CACHE_TTL_SECONDS = 6 * 3600  # 同一檔股票6小時內重複開圖/分析不用再打一次 yfinance


def _get_history_payload(symbol: str, period: str = "5y", interval: str = "1wk") -> dict:
    """核心邏輯：抓歷史股價（含快取）。失敗時丟出一般 Exception，由呼叫端決定如何處理。"""
    symbol = symbol.strip().upper()
    cache_key = f"{symbol}:{period}:{interval}"

    with _chart_cache_lock:
        cached = _chart_cache.get(cache_key)
        if cached and (time.time() - cached[0]) < CHART_CACHE_TTL_SECONDS:
            return cached[1]

    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, interval=interval)

    if hist is None or hist.empty:
        raise ValueError(f"查無 {symbol} 的歷史股價資料")

    def safe_float(v):
        return None if v is None or pd.isna(v) else round(float(v), 4)

    points = []
    for idx, row in hist.iterrows():
        points.append({
            "date": idx.strftime("%Y-%m-%d"),
            "open": safe_float(row.get("Open")),
            "high": safe_float(row.get("High")),
            "low": safe_float(row.get("Low")),
            "close": safe_float(row.get("Close")),
            "volume": None if pd.isna(row.get("Volume")) else int(row.get("Volume")),
        })

    payload = {"symbol": symbol, "period": period, "interval": interval, "points": points}
    with _chart_cache_lock:
        _chart_cache[cache_key] = (time.time(), payload)
    return payload


@app.get("/api/chart/{symbol}")
def get_chart(symbol: str, period: str = "5y", interval: str = "1wk"):
    try:
        return _get_history_payload(symbol, period, interval)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"取得 {symbol} 歷史股價失敗: {e}")


# ---------------------------------------------------------------------------
# 超速大盤策略 (Momentum Overdrive)
# ---------------------------------------------------------------------------
def _get_daily_close(symbol: str, on_date: date) -> Optional[float]:
    """抓 on_date 當天（或最近一個更早的交易日，例如遇到假日）的收盤價。"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=on_date - timedelta(days=10), end=on_date + timedelta(days=1), interval="1d")
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist[hist.index.date <= on_date]
    if hist.empty:
        return None
    return round(float(hist.iloc[-1]["Close"]), 4)


def _is_trading_day(symbol: str, on_date: date) -> bool:
    """
    直接問市場資料「這天真的有開盤嗎」，不只是排除週末——
    美股假日（感恩節、聖誕節等）雖然是平日，市場一樣休市，這裡會一併偵測到。
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=on_date, end=on_date + timedelta(days=1), interval="1d")
    except Exception:
        return False
    if hist is None or hist.empty:
        return False
    return any(d == on_date for d in hist.index.date)


def _get_daily_open(symbol: str, on_date: date) -> Optional[float]:
    """抓 on_date 當天（或往後找最近一個有資料的交易日）的開盤價。"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=on_date, end=on_date + timedelta(days=7), interval="1d")
    except Exception:
        return None
    if hist is None or hist.empty:
        return None
    hist = hist[hist.index.date >= on_date]
    if hist.empty:
        return None
    return round(float(hist.iloc[0]["Open"]), 4)


def _telegram_post(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = http_requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        return resp.ok
    except Exception as e:
        print(f"[telegram] 發送失敗: {e}")
        return False


def send_telegram_message(text: str) -> bool:
    """給排程/自動流程用：讀取已儲存的設定，且尊重 telegram_enabled 開關。"""
    settings = storage.load("momentum_settings")
    if not settings.get("telegram_enabled"):
        return False
    return _telegram_post(settings.get("telegram_bot_token"), settings.get("telegram_chat_id"), text)


def run_momentum_check() -> dict:
    """
    執行一次「讀值判斷」：抓基準ETF今日收盤價、計算動能指標、決定是否需要換倉。
    若需要換倉，只會記錄「預計換倉」，實際成交要等隔天 momentum_daily_job 抓到開盤價才會執行、
    對應原Excel「隔天日期／QLD開盤價」的兩階段流程。
    """
    today = datetime.now(timezone.utc).date()
    settings = storage.load("momentum_settings")
    state = storage.load("momentum_state")
    history = storage.load("momentum_history")

    price = _get_daily_close(settings["benchmark_symbol"], today)
    if price is None:
        raise RuntimeError(f"取得 {settings['benchmark_symbol']} 收盤價失敗，請稍後再試")

    calc = momentum.compute_momentum(history, price)
    row = {
        "date": today.isoformat(),
        "price": price,
        **calc,
        "execution_date": None,
        "execution_price": None,
        "shares_after": state["shares"],
        "cash_after": state["cash"],
        "position_value": None,
        "total_value": None,
        "cumulative_return": None,
        "note": None,
    }

    prev_position = "hold_stock" if state["shares"] > 0 else "hold_cash"
    needs_trade = calc["action"] is not None and calc["action"] != prev_position

    if needs_trade:
        exec_date = momentum.next_trading_day(today)
        state["pending_trade"] = {"execution_date": exec_date.isoformat(), "target_action": calc["action"]}
        row["execution_date"] = exec_date.isoformat()

    state["last_check_date"] = today.isoformat()
    state["last_check_month"] = today.strftime("%Y-%m")

    history.append(row)
    storage.save("momentum_history", history)
    storage.save("momentum_state", state)

    if needs_trade:
        action_label = momentum.ACTION_LABELS[calc["action"]]
        momentum_pct = f"{calc['momentum']*100:.1f}%" if calc["momentum"] is not None else "—"
        send_telegram_message(
            f"[超速大盤] 訊號更新：綜合動能指標 {momentum_pct}，建議「{action_label}」。\n"
            f"將於 {row['execution_date']} 開盤依 {settings['trade_symbol']} 開盤價自動記錄換倉，"
            f"請留意帳戶並視需要人工操作。"
        )

    return row


def momentum_daily_job():
    """每日排程：優先處理未完成的換倉（隔日開盤價執行），沒有的話再檢查是否輪到本月讀值判斷。"""
    try:
        today = datetime.now(timezone.utc).date()
        settings = storage.load("momentum_settings")
        state = storage.load("momentum_state")

        pending = state.get("pending_trade")
        if pending:
            exec_date = date.fromisoformat(pending["execution_date"])
            if today >= exec_date:
                open_price = _get_daily_open(settings["trade_symbol"], exec_date)
                if open_price is not None:
                    trade = momentum.execute_trade(
                        pending["target_action"], open_price, state["shares"], state["cash"]
                    )
                    state["shares"] = trade["shares_after"]
                    state["cash"] = trade["cash_after"]
                    state["position"] = "stock" if trade["shares_after"] > 0 else "cash"
                    state["pending_trade"] = None
                    storage.save("momentum_state", state)

                    history = storage.load("momentum_history")
                    if history:
                        h = history[-1]
                        h["execution_date"] = exec_date.isoformat()
                        h["execution_price"] = open_price
                        h["shares_after"] = trade["shares_after"]
                        h["cash_after"] = trade["cash_after"]
                        total_value = round(trade["shares_after"] * open_price + trade["cash_after"], 2)
                        h["position_value"] = round(trade["shares_after"] * open_price, 2)
                        h["total_value"] = total_value
                        ic = settings.get("initial_capital") or 0
                        h["cumulative_return"] = round((total_value - ic) / ic, 6) if ic else None
                        storage.save("momentum_history", history)

                    if trade["traded"]:
                        verb = "買進" if pending["target_action"] == "hold_stock" else "賣出"
                        send_telegram_message(
                            f"[超速大盤] 已於 {exec_date} 依開盤價 ${open_price:.2f} 完成{verb} "
                            f"{settings['trade_symbol']}，目前持有 {trade['shares_after']} 股，"
                            f"現金 ${trade['cash_after']:.2f}"
                        )
                return  # 這輪先處理完換倉執行，讀值判斷留給沒有 pending 時再做

        if not settings.get("auto_schedule_enabled"):
            return

        this_month = today.strftime("%Y-%m")
        if state.get("last_check_month") == this_month:
            return  # 這個月已經判斷過了

        target_day_num = min(max(int(settings.get("check_day_of_month", 11)), 1), 28)
        target_day = momentum.on_or_after_weekday(date(today.year, today.month, target_day_num))
        if today < target_day:
            return  # 還沒到本月的檢查日

        # 目標日可能剛好遇到平日休市的美股假日（感恩節、聖誕節等），不只是週末。
        # 這裡直接問市場資料「今天真的有開盤嗎」，沒開盤就先跳過，明天排程再檢查一次，
        # 直到遇到真正有開盤的日子才會執行讀值判斷，不會用到假日前的舊收盤價。
        if not _is_trading_day(settings["benchmark_symbol"], today):
            return  # 今天休市，明天排程會再確認一次

        run_momentum_check()
    except Exception as e:
        print(f"[momentum] 每日排程失敗: {e}")


@app.get("/api/momentum/settings")
def get_momentum_settings():
    return storage.load("momentum_settings")


@app.put("/api/momentum/settings")
def put_momentum_settings(settings: MomentumSettings):
    storage.save("momentum_settings", settings.dict())
    return settings.dict()


@app.get("/api/momentum/state")
def get_momentum_state():
    return storage.load("momentum_state")


@app.put("/api/momentum/state")
def override_momentum_state(override: MomentumStateOverride):
    """
    手動調整目前部位（例如像使用者這次一樣，主觀判斷要偏離規則自行出場/進場）。
    這不會跑動能計算，純粹是「告訴系統我實際上手動做了什麼」，
    同時會清掉任何待執行的自動換倉，避免系統隔天又依照舊訊號重複動作。
    """
    if override.position not in ("stock", "cash"):
        raise HTTPException(400, "position 必須是 'stock' 或 'cash'")

    state = storage.load("momentum_state")
    state["position"] = override.position
    state["shares"] = override.shares
    state["cash"] = override.cash
    state["pending_trade"] = None
    storage.save("momentum_state", state)

    if override.note:
        history = storage.load("momentum_history")
        if history:
            existing = history[-1].get("note", "")
            history[-1]["note"] = (existing + " / " if existing else "") + f"[手動override] {override.note}"
            storage.save("momentum_history", history)

    return state


@app.put("/api/momentum/history/{row_date}/note")
def set_momentum_history_note(row_date: str, payload: MomentumHistoryNote):
    history = storage.load("momentum_history")
    for row in history:
        if row.get("date") == row_date:
            row["note"] = payload.note
            storage.save("momentum_history", history)
            return row
    raise HTTPException(404, f"找不到日期為 {row_date} 的歷史紀錄")


@app.get("/api/momentum/history")
def get_momentum_history():
    return storage.load("momentum_history")


@app.post("/api/momentum/check-now")
def momentum_check_now_endpoint(force: bool = False):
    if force:
        state = storage.load("momentum_state")
        state["last_check_month"] = None
        storage.save("momentum_state", state)
    try:
        return run_momentum_check()
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/momentum/telegram-test")
def momentum_telegram_test(payload: Optional[MomentumTelegramTestRequest] = Body(default=None)):
    """
    測試按鈕直接送「畫面上目前的Token/Chat ID」（若有提供），不用先按「儲存設定」，
    也不要求 telegram_enabled 開關（測試本來就是為了在啟用前先確認能不能收到）。
    沒有從前端帶參數時，才退回讀已儲存的設定。
    """
    settings = storage.load("momentum_settings")
    token = (payload.bot_token if payload and payload.bot_token else settings.get("telegram_bot_token"))
    chat_id = (payload.chat_id if payload and payload.chat_id else settings.get("telegram_chat_id"))
    if not token or not chat_id:
        raise HTTPException(400, "請先填寫 Bot Token 與 Chat ID")
    ok = _telegram_post(token, chat_id, "[超速大盤] 這是一則測試訊息，收到代表 Telegram 通知設定成功。")
    if not ok:
        raise HTTPException(502, "傳送失敗，請確認 Bot Token / Chat ID 是否正確")
    return {"sent": True}


@app.post("/api/momentum/telegram-fetch-chat-id")
def fetch_telegram_chat_id(payload: dict = Body(...)):
    """
    自動抓 Chat ID，取代「使用者自己組 getUpdates 網址」的手動流程：
      1. 使用者先傳一則訊息（例如 /start）給自己的 Bot
      2. 這裡呼叫 getUpdates 找最新一筆訊息的 chat id
      3. 若該 Bot 之前設過 webhook，getUpdates 會失敗，這裡會自動先移除 webhook 再重試一次
    """
    token = (payload.get("bot_token") or "").strip()
    if not token:
        raise HTTPException(400, "請先輸入 Bot Token")

    base = f"https://api.telegram.org/bot{token}"

    def call_get_updates():
        resp = http_requests.get(f"{base}/getUpdates", timeout=10)
        return resp.json()

    try:
        data = call_get_updates()
    except Exception as e:
        raise HTTPException(502, f"連線 Telegram 失敗: {e}")

    if not data.get("ok"):
        desc = data.get("description", "")
        if "webhook" in desc.lower():
            # 常見情況：Bot 之前被設定過 webhook，getUpdates 無法使用，自動移除後重試一次
            try:
                http_requests.get(f"{base}/deleteWebhook", timeout=10)
                data = call_get_updates()
            except Exception as e:
                raise HTTPException(502, f"移除 webhook 後仍連線失敗: {e}")
        if not data.get("ok"):
            raise HTTPException(400, f"Telegram 回應錯誤：{desc or data.get('description', '未知錯誤')}，請確認 Bot Token 是否正確")

    results = data.get("result", [])
    if not results:
        raise HTTPException(
            404,
            "沒有偵測到任何訊息。請先在 Telegram 搜尋你的 Bot，傳一則訊息給它（例如輸入 /start），"
            "再按一次「自動取得 Chat ID」。",
        )

    last = results[-1]
    msg = last.get("message") or last.get("channel_post") or last.get("edited_message") or {}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    if chat_id is None:
        raise HTTPException(404, "抓到的訊息中沒有 chat id，請重新傳一則訊息給 Bot 後再試一次")

    display_name = chat.get("username") or chat.get("first_name") or chat.get("title") or str(chat_id)
    return {"chat_id": str(chat_id), "display_name": display_name}


@app.post("/api/momentum/import")
def import_momentum_history(req: MomentumImportRequest):
    rows_sorted = sorted(req.history, key=lambda r: r.date)
    history = []
    for r in rows_sorted:
        calc = momentum.compute_momentum(history, r.price)
        history.append({
            "date": r.date, "price": r.price, **calc,
            "execution_date": None, "execution_price": None,
            "shares_after": None, "cash_after": None,
            "position_value": None, "total_value": None, "cumulative_return": None,
            "note": None,
        })
    storage.save("momentum_history", history)

    state = {
        "position": "stock" if req.position == "stock" else "cash",
        "shares": req.shares,
        "cash": req.cash,
        "last_check_date": rows_sorted[-1].date if rows_sorted else None,
        "last_check_month": rows_sorted[-1].date[:7] if rows_sorted else None,
        "pending_trade": None,
    }
    storage.save("momentum_state", state)

    settings = storage.load("momentum_settings")
    settings["initial_capital"] = req.initial_capital
    storage.save("momentum_settings", settings)

    return {"imported_rows": len(history), "state": state}


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
