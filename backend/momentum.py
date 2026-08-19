"""
超速大盤策略引擎 (Momentum Overdrive Strategy Engine)
=========================================================
規則還原自使用者的策略文件與既有 Excel 紀錄表（含實際公式）：

  1. 每月固定一個檢查日，取得基準ETF（預設 QQQ）當日「調整後收盤價」。
  2. 計算三個績效指標：
       1個月績效 = (本次價格 - 上一次讀值價格) / 上一次讀值價格
       3個月績效 = (本次價格 - 前3次讀值價格) / 前3次讀值價格
       6個月績效 = (本次價格 - 前6次讀值價格) / 前6次讀值價格
     這裡的「前N次」是指「前N次月度讀值」，不是嚴格的日曆月份，
     與原始 Excel 公式 `=(B本次-B前N列)/B前N列` 完全一致。
     綜合動能指標 = 三者中「有算出來」的那幾個的平均值
     （歷史不足6個月時，跟 Excel 的 AVERAGE 一樣自動略過還沒有值的欄位）。
  3. 綜合動能指標 >= 0 → 持有／進場交易標的（預設 QLD）
     綜合動能指標 <  0 → 全數出場轉為現金
  4. 訊號變化在讀值當天判斷，實際交易在「下一個交易日」以交易標的的
     **開盤價**執行（對應原 Excel 的「隔天日期」「QLD開盤價」欄位）。
"""

from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# 交易日輔助（僅排除週末，不含美股假日；假日部分由抓不到報價時自然順延處理）
# ---------------------------------------------------------------------------
def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def next_trading_day(d: date) -> date:
    nd = d + timedelta(days=1)
    while not is_weekday(nd):
        nd += timedelta(days=1)
    return nd


def on_or_after_weekday(d: date) -> date:
    nd = d
    while not is_weekday(nd):
        nd += timedelta(days=1)
    return nd


# ---------------------------------------------------------------------------
# 核心動能計算
# ---------------------------------------------------------------------------
def _lookback_price(history: list, n: int) -> Optional[float]:
    """history 由舊到新排列；取倒數第 n 筆（不含本次）的價格。"""
    if len(history) < n:
        return None
    return history[-n]["price"]


def compute_momentum(history: list, current_price: float) -> dict:
    """
    history: 目前為止已經記錄的歷次讀值（不含本次），由舊到新排列，
             每筆至少要有 {"price": float}。
    回傳本次的績效指標與建議動作。
    """
    p1 = _lookback_price(history, 1)
    p3 = _lookback_price(history, 3)
    p6 = _lookback_price(history, 6)

    perf_1m = (current_price - p1) / p1 if p1 else None
    perf_3m = (current_price - p3) / p3 if p3 else None
    perf_6m = (current_price - p6) / p6 if p6 else None

    parts = [p for p in (perf_1m, perf_3m, perf_6m) if p is not None]
    momentum = sum(parts) / len(parts) if parts else None

    if momentum is None:
        action = None  # 歷史資料還不足以做出判斷（例如剛啟用系統的第一次讀值）
    elif momentum >= 0:
        action = "hold_stock"
    else:
        action = "hold_cash"

    return {
        "perf_1m": round(perf_1m, 6) if perf_1m is not None else None,
        "perf_3m": round(perf_3m, 6) if perf_3m is not None else None,
        "perf_6m": round(perf_6m, 6) if perf_6m is not None else None,
        "momentum": round(momentum, 6) if momentum is not None else None,
        "action": action,  # "hold_stock" | "hold_cash" | None
    }


ACTION_LABELS = {
    "hold_stock": "持有股票",
    "hold_cash": "持有現金",
    None: "資料不足",
}


def execute_trade(action: str, execution_price: float, prev_shares: float, prev_cash: float) -> dict:
    """
    依照決議的 action，用執行日的開盤價把部位換算成新的股數/現金。
    action="hold_stock" 且原本就是股票部位 → 維持股數不變（不會因為訊號連續為正而重複買進）。
    action="hold_cash"  且原本就是現金部位 → 維持現金不變。
    只有「部位真的要切換」時才會實際買賣。
    """
    prev_position = "hold_stock" if prev_shares > 0 else "hold_cash"

    if action == prev_position or action is None:
        # 沒有變化，或資料不足以判斷 → 維持現狀
        shares_after = prev_shares
        cash_after = prev_cash
        traded = False
    elif action == "hold_stock":
        # 轉為持有股票：把現金全部換成股票（無條件捨去到整股）
        shares_after = int(prev_cash // execution_price) if execution_price > 0 else 0
        leftover_cash = prev_cash - shares_after * execution_price
        cash_after = round(leftover_cash, 2)
        traded = True
    else:  # action == "hold_cash"
        # 轉為持有現金：把股票全部賣掉
        cash_after = round(prev_cash + prev_shares * execution_price, 2)
        shares_after = 0
        traded = True

    return {"shares_after": shares_after, "cash_after": cash_after, "traded": traded}
