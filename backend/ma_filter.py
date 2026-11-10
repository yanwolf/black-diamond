"""
併購（M&A）濾網
================
黑鑽選股的篩選重點之一：近期有併購相關消息的股票不選。原因是被收購的股票股價會鎖在收購價
附近，走勢看起來像「低位盤整後突破」，其實沒有後續動能；收購方則視情況而定。

判斷規則（對應使用者的偏好：兩種都排除，除非收購方對股價有利）：
  - 被收購方（target）          → 自動排除
  - 角色不明（unclear）         → 自動排除（保守起見，寧可多擋）
  - 收購方（acquirer）          → 保留在候選清單，但標「待判斷」，由人工看這筆收購對股價是否有利
  - 查不到任何證據（clear）     → 通過
  - 兩個資料來源都查詢失敗      → 保留並標「併購檢查失敗」（unknown），不會默默當作沒事
  多筆證據同時存在時，嚴格度高的優先：excluded > review > clear。

資料來源（兩個並用，互補）：
  1. SEC EDGAR 申報：官方、不靠關鍵字猜。看近 N 天有沒有收購要約、併購委託書、S-4、8-K 控制權變更等。
     只涵蓋「已簽約或已公告」的併購，傳聞階段看不到。
  2. 新聞標題關鍵字（yfinance Ticker.news）：能抓到傳聞，但覆蓋不穩、會誤判，所以只當輔助證據。

注意：EDGAR 要求每個請求帶可辨識的 User-Agent（含聯絡 email），請設定環境變數 SEC_USER_AGENT。
這個模組不保證抓全（洩漏消息、非英文來源都抓不到），是篩選輔助，不是取代人工判斷。
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from engine import with_retry

ENABLED = os.environ.get("MA_FILTER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
LOOKBACK_DAYS = int(os.environ.get("MA_LOOKBACK_DAYS", "90"))
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "BlackDiamondScreener/1.0 (personal research; set SEC_USER_AGENT with your email)"
)
SEC_REQUEST_DELAY = 0.2  # SEC 限制每秒 10 次請求，這裡保守一點

ROLE_TARGET = "target"
ROLE_ACQUIRER = "acquirer"
ROLE_UNCLEAR = "unclear"

STATUS_CLEAR = "clear"
STATUS_REVIEW = "review"
STATUS_EXCLUDED = "excluded"
STATUS_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# EDGAR 申報分類
# ---------------------------------------------------------------------------
# 公司自己的申報清單裡出現這些表單，代表它是被收購（或被私有化）的一方
_TARGET_FORMS = {"SC 14D9", "SC TO-T", "SC 13E3"}
# 併購委託書：通常是被收購方請股東表決，但發行股票的收購方也可能出現 → 角色不明
_UNCLEAR_FORMS = {"PREM14A", "DEFM14A", "425"}
# 為併購發行股票的註冊聲明，由收購方（發行方）提交
_ACQUIRER_FORMS = {"S-4", "F-4"}


def classify_filing(form: str, items: str = "") -> Optional[tuple]:
    """回傳 (role, 說明) 或 None（與併購無關的申報）。amendments（例如 'SC 14D9/A'）視同原表單。"""
    base = (form or "").upper().split("/")[0].strip()
    item_set = {i.strip() for i in (items or "").split(",") if i.strip()}

    if base in _TARGET_FORMS:
        return ROLE_TARGET, f"{form}（被收購/私有化相關申報）"
    if base in _ACQUIRER_FORMS:
        return ROLE_ACQUIRER, f"{form}（為併購發行股票的註冊聲明，通常是收購方）"
    if base in _UNCLEAR_FORMS:
        return ROLE_UNCLEAR, f"{form}（併購相關申報，角色需人工確認）"
    if base == "8-K":
        if "5.01" in item_set:
            return ROLE_TARGET, "8-K Item 5.01（公司控制權變更）"
        if "2.01" in item_set:
            return ROLE_UNCLEAR, "8-K Item 2.01（完成資產收購或處分，角色需人工確認）"
    return None


# ---------------------------------------------------------------------------
# 新聞標題分類
# ---------------------------------------------------------------------------
_TARGET_PHRASES = [
    "to be acquired", "to be bought", "to be taken private", "agrees to be", "acquired by",
    "bought by", "take-private", "take private", "going private", "tender offer",
    "buyout", "to be purchased", "to be sold to", "sale of the company", "to merge with",
]
_UNCLEAR_PHRASES = [
    "merger", "takeover", "strategic alternatives", "unsolicited offer", "business combination",
]
_ACQUIRER_VERBS = [
    "to acquire", "acquires", "agrees to acquire", "completes acquisition", "completes the acquisition",
    "announces acquisition", "to buy", "buys", "acquisition of", "to purchase",
]
_NAME_SUFFIXES = {
    "inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "ltd", "ltd.", "limited", "plc",
    "holdings", "holding", "group", "company", "the", "llc", "lp", "sa", "nv", "ag", "&",
}


def _name_key(name: Optional[str]) -> Optional[str]:
    """公司名稱的比對關鍵字：去掉 Inc/Corp 之類後綴，取前兩個字。找不到就回 None。"""
    if not name:
        return None
    words = [w for w in re.split(r"[\s,]+", name.lower()) if w and w not in _NAME_SUFFIXES]
    if not words:
        return None
    return " ".join(words[:2])


def classify_headline(title: str, name: Optional[str] = None, symbol: Optional[str] = None) -> Optional[tuple]:
    """
    回傳 (role, 說明) 或 None。
    被動語態（to be acquired / acquired by…）→ 被收購方，直接排除。
    主動語態（X to acquire Y）→ 看公司名稱出現在動詞前（收購方）還是後（被收購方）；
    找不到公司名稱就當角色不明，一樣排除。
    """
    t = (title or "").lower()
    if not t:
        return None

    for p in _TARGET_PHRASES:
        if p in t:
            return ROLE_TARGET, f"新聞標題含「{p}」"

    verb_hit = next((v for v in _ACQUIRER_VERBS if v in t), None)
    if verb_hit:
        verb_idx = t.index(verb_hit)
        key = _name_key(name)
        sym = (symbol or "").lower()
        name_idx = t.find(key) if key else -1
        if name_idx < 0 and sym:
            m = re.search(rf"[\(:\s]{re.escape(sym)}[\)\s:]", t)
            name_idx = m.start() if m else -1
        if name_idx >= 0:
            if name_idx < verb_idx:
                return ROLE_ACQUIRER, f"新聞標題「{verb_hit}」，公司在動詞前（推測為收購方）"
            return ROLE_TARGET, f"新聞標題「{verb_hit}」，公司在動詞後（推測為被收購方）"
        return ROLE_UNCLEAR, f"新聞標題含「{verb_hit}」，無法判斷公司是收購方或被收購方"

    for p in _UNCLEAR_PHRASES:
        if p in t:
            return ROLE_UNCLEAR, f"新聞標題含「{p}」"
    return None


# ---------------------------------------------------------------------------
# 證據彙整
# ---------------------------------------------------------------------------
def decide(evidence: list) -> tuple:
    """依嚴格度彙整：任何 target/unclear → excluded；否則有 acquirer → review；否則 clear。回傳 (status, role)。"""
    roles = {e["role"] for e in evidence}
    if ROLE_TARGET in roles:
        return STATUS_EXCLUDED, ROLE_TARGET
    if ROLE_UNCLEAR in roles:
        return STATUS_EXCLUDED, ROLE_UNCLEAR
    if ROLE_ACQUIRER in roles:
        return STATUS_REVIEW, ROLE_ACQUIRER
    return STATUS_CLEAR, None


# ---------------------------------------------------------------------------
# 資料來源：SEC EDGAR
# ---------------------------------------------------------------------------
_ticker_map = {"loaded_at": 0.0, "data": {}}
_ticker_lock = threading.Lock()


def _sec_get(url: str):
    resp = requests.get(url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}, timeout=15)
    resp.raise_for_status()
    time.sleep(SEC_REQUEST_DELAY)
    return resp.json()


def _cik_for(symbol: str) -> Optional[int]:
    with _ticker_lock:
        if time.time() - _ticker_map["loaded_at"] > 24 * 3600 or not _ticker_map["data"]:
            raw = with_retry(lambda: _sec_get("https://www.sec.gov/files/company_tickers.json"))
            _ticker_map["data"] = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
            _ticker_map["loaded_at"] = time.time()
        return _ticker_map["data"].get(symbol.upper().replace(".", "-"))


def fetch_edgar_evidence(symbol: str, lookback_days: int = LOOKBACK_DAYS) -> Optional[list]:
    """回傳證據清單；SEC 上查不到這個代碼（例如部分 ADR）時回 None（代表「沒檢查到」，不是「沒事」）。"""
    cik = _cik_for(symbol)
    if cik is None:
        return None
    data = with_retry(lambda: _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    items = recent.get("items") or [""] * len(forms)
    accs = recent.get("accessionNumber") or [""] * len(forms)
    docs = recent.get("primaryDocument") or [""] * len(forms)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()

    out = []
    for form, d, it, acc, doc in zip(forms, dates, items, accs, docs):
        if d < since:
            continue
        hit = classify_filing(form, it)
        if hit:
            role, note = hit
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}" if acc and doc else None
            out.append({"source": "edgar", "role": role, "date": d, "text": note, "url": url})
    return out


# ---------------------------------------------------------------------------
# 資料來源：新聞標題（yfinance）
# ---------------------------------------------------------------------------
def _parse_news_item(item: dict) -> tuple:
    """相容 yfinance 新舊兩種新聞格式，回傳 (title, datetime|None, url|None)。"""
    content = item.get("content") if isinstance(item.get("content"), dict) else None
    if content:
        title = content.get("title")
        pub = content.get("pubDate") or content.get("displayTime")
        url = ((content.get("canonicalUrl") or {}).get("url")) or ((content.get("clickThroughUrl") or {}).get("url"))
        dt = None
        if pub:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        return title, dt, url
    ts = item.get("providerPublishTime")
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else None
    return item.get("title"), dt, item.get("link")


def fetch_news_evidence(symbol: str, name: Optional[str] = None, lookback_days: int = LOOKBACK_DAYS) -> list:
    import yfinance as yf

    items = with_retry(lambda: yf.Ticker(symbol).news) or []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    out = []
    for item in items:
        title, dt, url = _parse_news_item(item)
        if not title:
            continue
        if dt is not None and dt < cutoff:
            continue
        hit = classify_headline(title, name=name, symbol=symbol)
        if hit:
            role, note = hit
            out.append({
                "source": "news", "role": role, "date": dt.date().isoformat() if dt else None,
                "text": f"{note}：{title}", "url": url,
            })
    return out


# ---------------------------------------------------------------------------
# 對候選股執行濾網
# ---------------------------------------------------------------------------
def check_candidate(candidate: dict, edgar_fetch: Optional[Callable] = None, news_fetch: Optional[Callable] = None) -> dict:
    """對單一候選股查併購證據，把 ma_* 欄位寫進 candidate 並回傳。edgar_fetch/news_fetch 可注入（測試用）。"""
    edgar_fetch = edgar_fetch or fetch_edgar_evidence
    news_fetch = news_fetch or fetch_news_evidence
    symbol = candidate.get("symbol")

    evidence, errors, edgar_covered = [], [], True
    try:
        e = edgar_fetch(symbol)
        if e is None:
            edgar_covered = False
        else:
            evidence.extend(e)
    except Exception as ex:
        errors.append(f"EDGAR: {ex}")
        edgar_covered = False
    try:
        evidence.extend(news_fetch(symbol, candidate.get("name")))
    except Exception as ex:
        errors.append(f"新聞: {ex}")

    status, role = decide(evidence)
    notes = []
    if status == STATUS_CLEAR:
        if len(errors) == 2:
            status = STATUS_UNKNOWN
            notes.append("兩個資料來源都查詢失敗，未能確認有無併購消息")
        else:
            if errors:
                notes.append("部分資料來源失敗（" + "；".join(errors) + "），結果可能不完整")
            if not edgar_covered and not errors:
                notes.append("SEC EDGAR 查無此代碼（可能是 ADR），僅用新聞檢查")
    candidate.update({
        "ma_status": status,
        "ma_role": role,
        "ma_evidence": evidence,
        "ma_note": "；".join(notes) if notes else None,
    })
    return candidate


def screen_candidates(candidates: list, edgar_fetch=None, news_fetch=None, progress: Optional[Callable] = None) -> tuple:
    """回傳 (kept, excluded)。kept 含 clear / review / unknown；excluded 是被自動排除的。"""
    kept, excluded = [], []
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        if progress:
            progress(i, total, c.get("symbol"))
        check_candidate(c, edgar_fetch=edgar_fetch, news_fetch=news_fetch)
        (excluded if c["ma_status"] == STATUS_EXCLUDED else kept).append(c)
    return kept, excluded
