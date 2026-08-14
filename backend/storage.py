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
        "interval_hours": 24,
        "last_run_at": None,
        "last_result_count": None,
        "next_run_at": None,
    },
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
