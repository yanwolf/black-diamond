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
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime
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
# 單檔股票評估
# ---------------------------------------------------------------------------
def evaluate_symbol(symbol: str, cfg: ScreenerConfig = DEFAULT_CONFIG) -> CandidateResult:
    fail_reasons = []
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
    except Exception as e:
        return CandidateResult(symbol=symbol, passed=False, fail_reasons=[f"資料取得失敗: {e}"])

    name = info.get("shortName") or info.get("longName")
    exchange = info.get("exchange") or info.get("fullExchangeName")
    market_cap = info.get("marketCap")
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    price_to_sales = info.get("priceToSalesTrailing12Months")
    week52_low = info.get("fiftyTwoWeekLow")
    week52_high = info.get("fiftyTwoWeekHigh")
    avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day")

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

    if cfg.exclude_otc and exchange and "OTC" in exchange.upper():
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


# ---------------------------------------------------------------------------
# 批次執行
# ---------------------------------------------------------------------------
def run_screener(symbols: list, cfg: ScreenerConfig = DEFAULT_CONFIG, verbose: bool = True,
                  progress_callback=None) -> dict:
    """
    progress_callback(i, total, symbol, passed_so_far) 會在每檔股票處理完後被呼叫，
    供後端 API 回報即時進度（例如寫入 job 狀態供前端輪詢）。
    """
    results = []
    total = len(symbols)
    passed_so_far = 0
    for i, sym in enumerate(symbols, 1):
        if verbose and i % 25 == 0:
            print(f"[{i}/{total}] 處理中... 最新: {sym}", file=sys.stderr)
        result = evaluate_symbol(sym, cfg)
        results.append(result)
        if result.passed:
            passed_so_far += 1
        if progress_callback:
            try:
                progress_callback(i, total, sym, passed_so_far)
            except Exception:
                pass
        time.sleep(cfg.request_delay_sec)

    passed = [asdict(r) for r in results if r.passed]
    # AMEX 需人工複核，故標註出來但不自動排除
    for c in passed:
        if c.get("exchange") and "AMEX" in c["exchange"].upper():
            c["needs_manual_review"] = "AMEX 交易所，請人工複核是否排除"

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "config": asdict(cfg),
        "universe_size": total,
        "candidate_count": len(passed),
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

    output = run_screener(symbols, cfg)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n完成！從 {output['universe_size']} 檔中篩選出 {output['candidate_count']} 檔候選股", file=sys.stderr)
    print(f"結果已寫入 {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
