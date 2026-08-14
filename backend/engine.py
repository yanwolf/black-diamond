"""
黑鑽選股引擎 (Black Diamond Screener Engine)
==============================================
根據「黑鑽選股SOP」實作的美股篩選引擎。

篩選邏輯（6道濾網）：
    1. 市值 (Market Cap)      : 25M ~ 250M（可調）
    2. 價值因子 (P/S)          : Price/Sales <= 1
    3. 動能因子 (Momentum)     : 股價位於52週區間 >= 90%
    4. 交易量 (Volume)         : 平均日成交量 >= 10,000
    5. 交易所 (Exchange)       : 排除 OTC（AMEX 留待人工複核）
    6. 股價 (Price)            : Price >= 1

用法：
    python engine.py --universe universe.csv --out candidates.json
    python engine.py --symbols AAPL,MSFT,XYZ --out candidates.json

    universe.csv 需有一欄 "Symbol"（可用 NASDAQ/NYSE 上市清單）。
    NASDAQ Trader 每日更新的免費清單（無需授權）：
        https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
        https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

注意：本引擎依賴 yfinance 連線至 Yahoo Finance，執行環境需能連外網。
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

try:
    import yfinance as yf
except ImportError:
    print("請先安裝 yfinance: pip install yfinance --break-system-packages", file=sys.stderr)
    raise

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# 濾網參數（可依需求調整，對應 SOP 文件中「這些參數不是固定不變」的提醒）
# ---------------------------------------------------------------------------
@dataclass
class ScreenerConfig:
    market_cap_min: float = 25_000_000
    market_cap_max: float = 250_000_000
    ps_ratio_max: float = 1.0
    range_position_min: float = 0.90       # 股價在52週高低區間的位置 (0~1)
    avg_volume_min: float = 10_000
    price_min: float = 1.0
    exclude_otc: bool = True
    request_delay_sec: float = 0.3          # 避免對 Yahoo Finance 送出過快請求


DEFAULT_CONFIG = ScreenerConfig()


@dataclass
class CandidateResult:
    symbol: str
    passed: bool
    fail_reasons: list
    name: Optional[str] = None
    exchange: Optional[str] = None
    market_cap: Optional[float] = None
    price: Optional[float] = None
    price_to_sales: Optional[float] = None
    week52_low: Optional[float] = None
    week52_high: Optional[float] = None
    range_position_pct: Optional[float] = None   # 位於52週區間的百分比
    avg_volume: Optional[float] = None


# ---------------------------------------------------------------------------
# 資料抓取
# ---------------------------------------------------------------------------
def fetch_nasdaq_universe() -> list:
    """從 NASDAQ Trader 免費清單抓取上市代碼（NASDAQ + 其他交易所）。"""
    if requests is None:
        raise RuntimeError("需要 requests 套件: pip install requests --break-system-packages")

    symbols = []
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]
    for url in urls:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        reader = csv.reader(io.StringIO(resp.text), delimiter="|")
        header = next(reader)
        symbol_idx = header.index("Symbol") if "Symbol" in header else header.index("ACT Symbol")
        etf_idx = header.index("ETF") if "ETF" in header else None
        for row in reader:
            if not row or len(row) <= symbol_idx:
                continue
            if row[0].startswith("File Creation Time"):
                continue
            if etf_idx is not None and etf_idx < len(row) and row[etf_idx] == "Y":
                continue  # 排除 ETF，只留個股
            symbols.append(row[symbol_idx])
    return sorted(set(s for s in symbols if s and "$" not in s))


def load_universe_csv(path: str) -> list:
    symbols = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("Symbol") or row.get("symbol")
            if sym:
                symbols.append(sym.strip())
    return symbols


# ---------------------------------------------------------------------------
# 型態分析（輔助人工判斷「低位盤整 ≥ 2年、剛突破起漲」，非取代人工判斷）
# ---------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def analyze_pattern(closes: list, weeks_per_year: int = 52, base_years: float = 2.0,
                     exclude_recent_weeks: int = 12) -> dict:
    """
    輸入：closes = 由舊到新排列的收盤價（建議用週K，約需 base_years+3個月 的資料量）。

    邏輯：
      1. 排除最近 exclude_recent_weeks（約3個月）視為「起漲/觀察窗」。
      2. 往前抓 base_years（預設2年）當作「盤整基期」。
      3. 基期的價格區間越窄（(最高-最低)/最低）分數越高 → 代表盤整夠緊。
      4. 基期頭尾價格變化越接近 0% 分數越高 → 代表是真的橫盤，不是緩漲。
      5. 觀察窗的最高價要站上基期最高價，才算「確認突破」。
      6. 突破幅度不能超過基期高點太多，太多代表已經噴出一段，不是「剛」突破。

    這是輔助排序用的量化指標，仍需人工用「看線圖」功能肉眼複核，
    無法判斷除權息調整是否正確、有無假突破、是否為剛IPO或有重大事件干擾等情況。
    """
    base_weeks = int(base_years * weeks_per_year)
    needed = base_weeks + exclude_recent_weeks

    if not closes or len(closes) < needed * 0.6:  # 資料量明顯不足（例如剛上市不久）
        return {
            "pattern_score": None,
            "base_range_pct": None,
            "base_trend_pct": None,
            "breakout_confirmed": None,
            "breakout_extension_pct": None,
            "pattern_note": f"歷史資料不足（需要約 {base_years:.1f} 年以上），可能是近期新上市或資料不完整，無法自動分析型態",
        }

    # 資料量不到完整需求但還堪用時，依實際可用長度等比例縮小基期，仍嘗試分析
    usable = closes[-min(len(closes), needed):]
    if len(usable) <= exclude_recent_weeks + 4:
        return {
            "pattern_score": None,
            "base_range_pct": None,
            "base_trend_pct": None,
            "breakout_confirmed": None,
            "breakout_extension_pct": None,
            "pattern_note": "資料量過短，無法區分基期與觀察窗",
        }

    recent = usable[-exclude_recent_weeks:]
    base = usable[:-exclude_recent_weeks]

    base_high = max(base)
    base_low = min(base)
    if base_low <= 0 or base_high <= 0:
        return {
            "pattern_score": None,
            "base_range_pct": None,
            "base_trend_pct": None,
            "breakout_confirmed": None,
            "breakout_extension_pct": None,
            "pattern_note": "價格資料異常，無法分析",
        }

    base_range_pct = (base_high - base_low) / base_low * 100
    base_trend_pct = (base[-1] - base[0]) / base[0] * 100

    recent_high = max(recent)
    breakout_confirmed = recent_high > base_high
    breakout_extension_pct = (recent_high - base_high) / base_high * 100

    # --- 四個子分數，各占25分 ---
    # 1. 基期夠緊：範圍 <=40% 給滿分，>=150% 給0分
    tightness_score = _clamp(25 * (1 - (base_range_pct - 40) / (150 - 40)), 0, 25)

    # 2. 基期夠平：頭尾變化在0%附近給滿分，>=60%（不論漲跌）給0分
    flatness_score = _clamp(25 * (1 - abs(base_trend_pct) / 60), 0, 25)

    # 3. 是否確認突破基期高點
    breakout_score = 25 if breakout_confirmed else 0

    # 4. 突破幅度別噴太多：0%~15%給滿分，>=50%給0分（沒突破則此項為0）
    if breakout_confirmed:
        extension_score = _clamp(25 * (1 - max(0, breakout_extension_pct - 15) / (50 - 15)), 0, 25)
    else:
        extension_score = 0

    # 突破分數要用「基期品質」打折：如果基期本來就不夠緊、不夠平（例如根本是持續緩漲，
    # 不是真正的橫盤蓄積），就算價格創了新高，也不該被當成典型的黑鑽起漲型態。
    base_component = tightness_score + flatness_score       # 0~50
    base_quality_ratio = base_component / 50                # 0~1
    breakout_component = breakout_score + extension_score   # 0~50

    pattern_score = round(base_component + base_quality_ratio * breakout_component)
    pattern_score = int(_clamp(pattern_score, 0, 100))

    if pattern_score >= 70:
        note = "型態符合度高：基期夠緊、夠平，且剛確認突破"
    elif pattern_score >= 40:
        note = "型態普通，建議搭配線圖肉眼複核"
    else:
        note = "型態偏弱（基期太寬鬆、走勢已偏離盤整、或尚未確認突破），建議謹慎看待"

    return {
        "pattern_score": pattern_score,
        "base_range_pct": round(base_range_pct, 1),
        "base_trend_pct": round(base_trend_pct, 1),
        "breakout_confirmed": breakout_confirmed,
        "breakout_extension_pct": round(breakout_extension_pct, 1),
        "pattern_note": note,
    }


# ---------------------------------------------------------------------------
# 單檔股票評估
# ---------------------------------------------------------------------------
def _to_float(val):
    """安全轉成 float；yfinance 對少數股票偶爾會回傳字串、'Infinity'、或其他非數值型態。"""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / Infinity
        return None
    return f


def evaluate_symbol(symbol: str, cfg: ScreenerConfig = DEFAULT_CONFIG) -> CandidateResult:
    fail_reasons = []
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
    except Exception as e:
        return CandidateResult(symbol=symbol, passed=False, fail_reasons=[f"資料取得失敗: {e}"])

    try:
        name = info.get("shortName") or info.get("longName")
        exchange = info.get("exchange") or info.get("fullExchangeName")
        market_cap = _to_float(info.get("marketCap"))
        price = _to_float(info.get("currentPrice") or info.get("regularMarketPrice"))
        price_to_sales = _to_float(info.get("priceToSalesTrailing12Months"))
        week52_low = _to_float(info.get("fiftyTwoWeekLow"))
        week52_high = _to_float(info.get("fiftyTwoWeekHigh"))
        avg_volume = _to_float(info.get("averageVolume") or info.get("averageDailyVolume10Day"))

        range_position_pct = None
        if price is not None and week52_low is not None and week52_high is not None and week52_high > week52_low:
            range_position_pct = (price - week52_low) / (week52_high - week52_low)

        # --- 套用6道濾網 ---
        if market_cap is None:
            fail_reasons.append("無市值資料")
        elif not (cfg.market_cap_min <= market_cap <= cfg.market_cap_max):
            fail_reasons.append(f"市值 {market_cap:,.0f} 不在 {cfg.market_cap_min:,.0f}~{cfg.market_cap_max:,.0f} 範圍")

        if price_to_sales is None:
            fail_reasons.append("無 P/S 資料")
        elif price_to_sales > cfg.ps_ratio_max:
            fail_reasons.append(f"P/S {price_to_sales:.2f} 超過 {cfg.ps_ratio_max}")

        if range_position_pct is None:
            fail_reasons.append("無法計算52週區間位置")
        elif range_position_pct < cfg.range_position_min:
            fail_reasons.append(f"52週區間位置 {range_position_pct*100:.1f}% 低於 {cfg.range_position_min*100:.0f}%")

        if avg_volume is None:
            fail_reasons.append("無成交量資料")
        elif avg_volume < cfg.avg_volume_min:
            fail_reasons.append(f"平均成交量 {avg_volume:,.0f} 低於 {cfg.avg_volume_min:,.0f}")

        if price is None:
            fail_reasons.append("無股價資料")
        elif price < cfg.price_min:
            fail_reasons.append(f"股價 {price} 低於 {cfg.price_min}")

        if cfg.exclude_otc and exchange and "OTC" in str(exchange).upper():
            fail_reasons.append("交易所為 OTC")

        return CandidateResult(
            symbol=symbol,
            passed=len(fail_reasons) == 0,
            fail_reasons=fail_reasons,
            name=name,
            exchange=exchange,
            market_cap=market_cap,
            price=price,
            price_to_sales=price_to_sales,
            week52_low=week52_low,
            week52_high=week52_high,
            range_position_pct=round(range_position_pct * 100, 2) if range_position_pct is not None else None,
            avg_volume=avg_volume,
        )
    except Exception as e:
        # 單一股票的資料異常（型態不符、缺欄位等）不應讓整批掃描中斷，
        # 記錄成該股票的失敗原因即可，其餘股票繼續處理。
        return CandidateResult(symbol=symbol, passed=False, fail_reasons=[f"評估時發生錯誤: {e}"])


# ---------------------------------------------------------------------------
# 個股快取（記錄上次檢查時間，避免短時間內重複打 Yahoo Finance）
# ---------------------------------------------------------------------------
def _cache_is_fresh(entry: dict, freshness_hours: float) -> bool:
    checked_at = entry.get("checked_at")
    if not checked_at:
        return False
    try:
        checked_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except Exception:
        return False
    if checked_dt.tzinfo is None:
        checked_dt = checked_dt.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - checked_dt).total_seconds() / 3600
    return age_hours < freshness_hours


def _result_from_cache_entry(symbol: str, entry: dict) -> CandidateResult:
    return CandidateResult(
        symbol=symbol,
        passed=entry.get("passed", False),
        fail_reasons=entry.get("fail_reasons", []),
        name=entry.get("name"),
        exchange=entry.get("exchange"),
        market_cap=entry.get("market_cap"),
        price=entry.get("price"),
        price_to_sales=entry.get("price_to_sales"),
        week52_low=entry.get("week52_low"),
        week52_high=entry.get("week52_high"),
        range_position_pct=entry.get("range_position_pct"),
        avg_volume=entry.get("avg_volume"),
    )


# ---------------------------------------------------------------------------
# 批次執行
# ---------------------------------------------------------------------------
def run_screener(symbols: list, cfg: ScreenerConfig = DEFAULT_CONFIG, verbose: bool = True,
                  progress_callback=None, cache: Optional[dict] = None,
                  freshness_hours: float = 24.0) -> dict:
    """
    progress_callback(i, total, symbol, passed_so_far, from_cache) 會在每檔股票處理完後被呼叫，
    供後端 API 回報即時進度。

    cache: 一個 {symbol: {...評估結果欄位..., "checked_at": iso時間}} 的字典（會被就地修改）。
           若某檔股票在 cache 內且 checked_at 距今小於 freshness_hours，就直接沿用快取結果，
           不會再對 Yahoo Finance 發送請求，也不會計入速率限制的 sleep。
    """
    if cache is None:
        cache = {}

    results = []
    total = len(symbols)
    passed_so_far = 0
    cache_hits = 0
    fresh_fetches = 0

    for i, sym in enumerate(symbols, 1):
        entry = cache.get(sym)
        from_cache = entry is not None and _cache_is_fresh(entry, freshness_hours)

        try:
            if from_cache:
                result = _result_from_cache_entry(sym, entry)
                cache_hits += 1
            else:
                if verbose and i % 25 == 0:
                    print(f"[{i}/{total}] 處理中... 最新: {sym}", file=sys.stderr)
                result = evaluate_symbol(sym, cfg)
                cache[sym] = {**asdict(result), "checked_at": datetime.utcnow().isoformat() + "Z"}
                fresh_fetches += 1
                time.sleep(cfg.request_delay_sec)  # 只有真正打 API 才需要限速
        except Exception as e:
            # 保底：就算 evaluate_symbol 內部的防呆沒接住，也不能讓單一股票拖垮整批掃描
            result = CandidateResult(symbol=sym, passed=False, fail_reasons=[f"處理時發生未預期錯誤: {e}"])
            fresh_fetches += 1

        results.append(result)
        if result.passed:
            passed_so_far += 1
        if progress_callback:
            try:
                progress_callback(i, total, sym, passed_so_far, from_cache)
            except Exception:
                pass

    passed = [asdict(r) for r in results if r.passed]
    # AMEX 需人工複核，故標註出來但不自動排除
    for c in passed:
        if c.get("exchange") and "AMEX" in str(c["exchange"]).upper():
            c["needs_manual_review"] = "AMEX 交易所，請人工複核是否排除"

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "config": asdict(cfg),
        "universe_size": total,
        "candidate_count": len(passed),
        "cache_hits": cache_hits,
        "fresh_fetches": fresh_fetches,
        "candidates": sorted(passed, key=lambda x: x.get("range_position_pct") or 0, reverse=True),
    }


def main():
    parser = argparse.ArgumentParser(description="黑鑽選股引擎")
    parser.add_argument("--universe", help="股票清單 CSV 檔路徑（需含 Symbol 欄位）")
    parser.add_argument("--symbols", help="逗號分隔的股票代碼清單，例如 AAPL,MSFT")
    parser.add_argument("--fetch-nasdaq", action="store_true", help="自動抓取 NASDAQ/NYSE 完整上市清單")
    parser.add_argument("--out", default="candidates.json", help="輸出 JSON 檔路徑")
    parser.add_argument("--market-cap-min", type=float, default=DEFAULT_CONFIG.market_cap_min)
    parser.add_argument("--market-cap-max", type=float, default=DEFAULT_CONFIG.market_cap_max)
    parser.add_argument("--ps-max", type=float, default=DEFAULT_CONFIG.ps_ratio_max)
    parser.add_argument("--range-min", type=float, default=DEFAULT_CONFIG.range_position_min)
    parser.add_argument("--volume-min", type=float, default=DEFAULT_CONFIG.avg_volume_min)
    parser.add_argument("--cache-file", help="個股快取檔路徑，重複執行時可跳過近期已檢查過的股票")
    parser.add_argument("--cache-max-age-hours", type=float, default=24.0,
                         help="快取有效時數，超過這個時數的個股資料視為過期，會重新查詢")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe:
        symbols = load_universe_csv(args.universe)
    elif args.fetch_nasdaq:
        print("正在抓取 NASDAQ/NYSE 上市清單...", file=sys.stderr)
        symbols = fetch_nasdaq_universe()
        print(f"共取得 {len(symbols)} 檔股票", file=sys.stderr)
    else:
        parser.error("請指定 --symbols、--universe 或 --fetch-nasdaq 其中一項")
        return

    cfg = ScreenerConfig(
        market_cap_min=args.market_cap_min,
        market_cap_max=args.market_cap_max,
        ps_ratio_max=args.ps_max,
        range_position_min=args.range_min,
        avg_volume_min=args.volume_min,
    )

    cache = {}
    if args.cache_file and os.path.exists(args.cache_file):
        with open(args.cache_file, encoding="utf-8") as f:
            cache = json.load(f)

    output = run_screener(symbols, cfg, cache=cache, freshness_hours=args.cache_max_age_hours)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if args.cache_file:
        with open(args.cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"\n完成！從 {output['universe_size']} 檔中篩選出 {output['candidate_count']} 檔候選股", file=sys.stderr)
    print(f"（快取命中 {output['cache_hits']} 檔，實際查詢 {output['fresh_fetches']} 檔）", file=sys.stderr)
    print(f"結果已寫入 {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
