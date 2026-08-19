"""
簡易檔案儲存層 (Simple JSON file storage)
==========================================
用於持久化 settings / candidates / holdings，資料寫在 DATA_DIR 底下。

⚠️ Zeabur 部署注意：預設容器檔案系統重啟後會清空，若要讓資料長期保存，
   請在 Zeabur 專案設定中替此服務掛載一個 Volume，並把 DATA_DIR 指到該路徑
   （例如掛載至 /data，並設定環境變數 DATA_DIR=/data）。
"""

import json
import os
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

DEFAULTS = {
    "settings": {"totalCapital": 500000, "riskPct": 1},
    "candidates": {"generated_at": None, "candidates": []},
    "holdings": [],
    "schedule": {
        "enabled": False,
        "interval_hours": 168,   # 預設每週掃描一次
        "last_run_at": None,
        "last_result_count": None,
        "next_run_at": None,
    },
    "symbol_cache": {},
    "momentum_settings": {
        "benchmark_symbol": "QQQ",     # 用來計算動能指標的基準ETF
        "trade_symbol": "QLD",         # 實際交易標的
        "initial_capital": 0,
        "check_day_of_month": 11,      # 每月檢查日（若非交易日會順延到下一個交易日）
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "telegram_enabled": False,
        "auto_schedule_enabled": False,
    },
    "momentum_state": {
        "position": "cash",            # "stock" | "cash"
        "shares": 0,
        "cash": 0,
        "last_check_date": None,       # 最近一次「讀值判斷」的日期
        "last_check_month": None,      # "YYYY-MM"，避免同一個月重複判斷
        "pending_trade": None,         # {"execution_date":..., "target_action":...} 或 None
    },
    "momentum_history": [],
}


def _path(name: str) -> Path:
    return DATA_DIR / f"{name}.json"


def load(name: str):
    p = _path(name)
    if not p.exists():
        return DEFAULTS.get(name)
    with _lock:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DEFAULTS.get(name)


def save(name: str, value) -> None:
    p = _path(name)
    with _lock:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
