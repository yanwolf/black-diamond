"""
黑鑽飆股策略 — 持股追蹤與出場規則引擎
=========================================
欄位與公式還原自使用者的「黑鑽飆股策略紀錄表」Excel（含實際公式），
7 種出場原因逐字對應該表格「原因」欄位的下拉選單清單：

  1. 收盤價跌破止損線，全部出場
  2. 1個月未漲至停利線，出場1/2
  3. 2個月未漲至停利線，全部出場
  4. 盤中跌破SMA20，出場1/4
  5. 盤中跌破EMA60，出場1/2
  6. 盤中跌破EMA120，出場1/4
  7. 漲達停利線後跌回進場價，全部出場

規則優先順序（比照 SOP「所有出場行為都必須有規則依據」的精神，逐日檢查）：
  a. 收盤價 < 止損線 → 無條件全部出場（最高優先，只要還有未出清股數，每天都會提醒）
  b. 若「從未觸及過停利線」：
       持有 ≥ 60 天且只做過（或還沒做過）第一次減碼 → 2個月未達，全部出場
       持有 ≥ 30 天且完全沒出場過 → 1個月未達，出場1/2
  c. 若「曾經觸及過停利線」（等同止損拉高到進場價，改用均線分批出場）：
       現價 ≤ 進場價 且均線各關卡都還沒觸發過 → 漲回進場價，全部出場
       現價 < SMA20 → 出場1/4（若這關還沒提醒過）
       現價 < EMA60 → 出場1/2（若這關還沒提醒過）
       現價 < EMA120 → 出場1/4（若這關還沒提醒過）

這是提醒/輔助工具，不是自動下單：符合規則時發出通知，實際出場價格、股數、
執行時間仍由使用者自行操作並回填紀錄，跟原本 Excel 表「人工填入出場欄位」的精神一致。
"""

from dataclasses import dataclass
from typing import Optional, List


PROFIT_TARGET_MULTIPLIER = 1.2  # 停利線 = 進場價 * 1.2，跟 Excel 公式 E*1.2 一致

EXIT_REASONS = [
    "收盤價跌破止損線，全部出場",
    "1個月未漲至停利線，出場1/2",
    "2個月未漲至停利線，全部出場",
    "盤中跌破SMA20，出場1/4",
    "盤中跌破EMA60，出場1/2",
    "盤中跌破EMA120，出場1/4",
    "漲達停利線後跌回進場價，全部出場",
]


def strip_exchange_prefix(symbol: str) -> str:
    """"NASDAQ:OESX" -> "OESX"；yfinance 查價不需要交易所前綴。"""
    return symbol.split(":")[-1].strip().upper() if symbol else symbol


def profit_target(entry_price: float) -> float:
    return round(entry_price * PROFIT_TARGET_MULTIPLIER, 4)


def risk_reference_prices(entry_price: float) -> dict:
    """對應風險計算表的 -20%參考 / -25%參考，幫助手動決定止損線落在哪個支撐位。"""
    return {
        "ref_20pct": round(entry_price * 0.8, 4),
        "ref_25pct": round(entry_price * 0.75, 4),
    }


def suggested_shares(risk_amount: float, entry_price: float, stop_loss: float) -> int:
    """應買股數 = round(風險金額 / (進場價 - 止損線))，跟 Excel 公式一致。"""
    per_share_risk = entry_price - stop_loss
    if per_share_risk <= 0:
        return 0
    return round(risk_amount / per_share_risk)


def compute_financials(position: dict, current_price: Optional[float]) -> dict:
    """
    對應 Excel 的 Y~AF 欄位：未出清股數、買入/賣出金額、持有市值、
    未實現/已實現損益、損益比例、未實現+已實現。
    """
    shares = position.get("shares", 0)
    entry_price = position.get("entry_price", 0)
    exits = position.get("exits", []) or []

    sold_shares = sum(e.get("shares", 0) for e in exits)
    remaining_shares = round(shares - sold_shares, 6)

    buy_amount = round(entry_price * shares, 4)
    sell_amount = round(sum(e.get("price", 0) * e.get("shares", 0) for e in exits), 4)
    realized_pnl = round(sum((e.get("price", 0) - entry_price) * e.get("shares", 0) for e in exits), 4)

    market_value = round(remaining_shares * current_price, 4) if current_price is not None else None
    unrealized_pnl = round(remaining_shares * (current_price - entry_price), 4) if current_price is not None else None

    total_pnl = round((unrealized_pnl or 0) + realized_pnl, 4)
    pnl_ratio = round(total_pnl / buy_amount, 6) if buy_amount > 0 else None

    return {
        "remaining_shares": remaining_shares,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "market_value": market_value,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "pnl_ratio": pnl_ratio,
        "total_pnl": total_pnl,
    }


def evaluate_exit_rule(
    position: dict,
    current_price: Optional[float],
    holding_days: Optional[int],
    ever_hit_target: bool,
    sma20: Optional[float] = None,
    ema60: Optional[float] = None,
    ema120: Optional[float] = None,
) -> Optional[dict]:
    """
    回傳這次應該提醒使用者的規則，或 None（沒有新的規則被觸發）。
    呼叫端要自行檢查對應的 alerted_* 旗標避免同一關卡重複通知
    （止損線例外：只要沒平倉、現價持續在止損線之下，每天都會再提醒一次）。
    """
    fin = compute_financials(position, current_price)
    if fin["remaining_shares"] <= 0:
        return None  # 已經全部出清

    stop_loss = position.get("stop_loss")
    entry_price = position.get("entry_price")
    exits = position.get("exits", []) or []

    if current_price is not None and stop_loss is not None and current_price < stop_loss:
        return {"rule": "stop_loss", "reason": EXIT_REASONS[0], "fraction": 1.0}

    if not ever_hit_target:
        if holding_days is not None and holding_days >= 60 and len(exits) <= 1:
            return {"rule": "two_month", "reason": EXIT_REASONS[2], "fraction": 1.0}
        if (
            holding_days is not None
            and holding_days >= 30
            and len(exits) == 0
            and not position.get("alerted_1month")
        ):
            return {"rule": "one_month", "reason": EXIT_REASONS[1], "fraction": 0.5}
        return None

    # 已觸及過停利線：停損視同拉高到進場價，改用均線分批出場
    any_tier_alerted = position.get("alerted_sma20") or position.get("alerted_ema60") or position.get("alerted_ema120")
    if (
        current_price is not None
        and entry_price is not None
        and current_price <= entry_price
        and not any_tier_alerted
        and not position.get("alerted_fallback_entry")
    ):
        return {"rule": "fallback_entry", "reason": EXIT_REASONS[6], "fraction": 1.0}

    if sma20 is not None and current_price is not None and current_price < sma20 and not position.get("alerted_sma20"):
        return {"rule": "sma20", "reason": EXIT_REASONS[3], "fraction": 0.25}
    if ema60 is not None and current_price is not None and current_price < ema60 and not position.get("alerted_ema60"):
        return {"rule": "ema60", "reason": EXIT_REASONS[4], "fraction": 0.5}
    if ema120 is not None and current_price is not None and current_price < ema120 and not position.get("alerted_ema120"):
        return {"rule": "ema120", "reason": EXIT_REASONS[5], "fraction": 0.25}

    return None


def alert_flag_field(rule: str) -> Optional[str]:
    """觸發某規則後，要在 position 上設定哪個旗標避免重複通知（止損線不設旗標，每天都可再提醒）。"""
    return {
        "one_month": "alerted_1month",
        "two_month": "alerted_2month",
        "sma20": "alerted_sma20",
        "ema60": "alerted_ema60",
        "ema120": "alerted_ema120",
        "fallback_entry": "alerted_fallback_entry",
    }.get(rule)
