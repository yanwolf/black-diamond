"""
併購濾網的回歸測試：EDGAR 申報分類、新聞標題分類（被收購方/收購方）、證據彙整、
失敗處理、接進掃描流程與「救回」端點。

執行方式（在 bd-app/ 目錄下，需先安裝 backend/requirements.txt 與 httpx）：
    python -m unittest tests/test_ma_filter.py -v

全部用注入的假資料來源，不會連外網；DATA_DIR 指到暫存資料夾，不會碰到正式 Volume。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="bd-ma-test-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import ma_filter as mf  # noqa: E402
import main  # noqa: E402
import storage  # noqa: E402


def cand(symbol="ABCD", name="Abcd Therapeutics Inc."):
    return {"symbol": symbol, "name": name, "pattern_score": 60}


def no_news(symbol, name=None):
    return []


def no_edgar(symbol):
    return []


class ClassifyFilingTests(unittest.TestCase):
    def test_target_forms(self):
        for form in ("SC 14D9", "SC 14D9/A", "SC TO-T", "SC 13E3"):
            self.assertEqual(mf.classify_filing(form)[0], mf.ROLE_TARGET, form)

    def test_acquirer_forms(self):
        self.assertEqual(mf.classify_filing("S-4")[0], mf.ROLE_ACQUIRER)
        self.assertEqual(mf.classify_filing("S-4/A")[0], mf.ROLE_ACQUIRER)

    def test_unclear_forms(self):
        for form in ("DEFM14A", "PREM14A", "425"):
            self.assertEqual(mf.classify_filing(form)[0], mf.ROLE_UNCLEAR, form)

    def test_8k_items(self):
        self.assertEqual(mf.classify_filing("8-K", "5.01,9.01")[0], mf.ROLE_TARGET)
        self.assertEqual(mf.classify_filing("8-K", "2.01")[0], mf.ROLE_UNCLEAR)

    def test_unrelated_filings_ignored(self):
        self.assertIsNone(mf.classify_filing("10-Q"))
        self.assertIsNone(mf.classify_filing("8-K", "1.01"))  # 1.01 太常見（借貸合約等），不單獨算
        self.assertIsNone(mf.classify_filing("8-K", "7.01,9.01"))


class ClassifyHeadlineTests(unittest.TestCase):
    NAME = "Abcd Therapeutics Inc."

    def test_passive_means_target(self):
        r = mf.classify_headline("Abcd Therapeutics to be acquired by BigPharma for $12 per share", self.NAME, "ABCD")
        self.assertEqual(r[0], mf.ROLE_TARGET)

    def test_company_before_verb_is_acquirer(self):
        r = mf.classify_headline("Abcd Therapeutics to acquire SmallCo in all-cash deal", self.NAME, "ABCD")
        self.assertEqual(r[0], mf.ROLE_ACQUIRER)

    def test_company_after_verb_is_target(self):
        r = mf.classify_headline("BigPharma to acquire Abcd Therapeutics for $500 million", self.NAME, "ABCD")
        self.assertEqual(r[0], mf.ROLE_TARGET)

    def test_name_not_found_is_unclear(self):
        r = mf.classify_headline("XYZ Corp to acquire QRS Ltd", self.NAME, "ABCD")
        self.assertEqual(r[0], mf.ROLE_UNCLEAR)

    def test_ticker_fallback_when_name_missing(self):
        r = mf.classify_headline("(ABCD) completes acquisition of SmallCo", None, "ABCD")
        self.assertEqual(r[0], mf.ROLE_ACQUIRER)

    def test_unclear_keywords(self):
        self.assertEqual(mf.classify_headline("Abcd explores strategic alternatives", self.NAME, "ABCD")[0], mf.ROLE_UNCLEAR)
        self.assertEqual(mf.classify_headline("Takeover speculation lifts Abcd", self.NAME, "ABCD")[0], mf.ROLE_UNCLEAR)

    def test_unrelated_headline(self):
        self.assertIsNone(mf.classify_headline("Abcd reports Q3 results, revenue up 12%", self.NAME, "ABCD"))
        self.assertIsNone(mf.classify_headline("", self.NAME, "ABCD"))


class DecideTests(unittest.TestCase):
    def ev(self, *roles):
        return [{"role": r} for r in roles]

    def test_precedence(self):
        self.assertEqual(mf.decide([]), (mf.STATUS_CLEAR, None))
        self.assertEqual(mf.decide(self.ev(mf.ROLE_ACQUIRER)), (mf.STATUS_REVIEW, mf.ROLE_ACQUIRER))
        self.assertEqual(mf.decide(self.ev(mf.ROLE_ACQUIRER, mf.ROLE_UNCLEAR)), (mf.STATUS_EXCLUDED, mf.ROLE_UNCLEAR))
        self.assertEqual(mf.decide(self.ev(mf.ROLE_ACQUIRER, mf.ROLE_TARGET)), (mf.STATUS_EXCLUDED, mf.ROLE_TARGET))


class CheckCandidateTests(unittest.TestCase):
    def test_clear(self):
        c = mf.check_candidate(cand(), no_edgar, no_news)
        self.assertEqual(c["ma_status"], mf.STATUS_CLEAR)
        self.assertEqual(c["ma_evidence"], [])

    def test_target_from_edgar_is_excluded(self):
        edgar = lambda s: [{"source": "edgar", "role": mf.ROLE_TARGET, "date": "2026-10-01", "text": "SC 14D9", "url": None}]
        c = mf.check_candidate(cand(), edgar, no_news)
        self.assertEqual(c["ma_status"], mf.STATUS_EXCLUDED)
        self.assertEqual(c["ma_role"], mf.ROLE_TARGET)

    def test_acquirer_is_kept_for_review(self):
        edgar = lambda s: [{"source": "edgar", "role": mf.ROLE_ACQUIRER, "date": "2026-10-01", "text": "S-4", "url": None}]
        c = mf.check_candidate(cand(), edgar, no_news)
        self.assertEqual(c["ma_status"], mf.STATUS_REVIEW)

    def test_both_sources_fail_is_unknown_not_clear(self):
        def boom_edgar(s):
            raise RuntimeError("sec down")

        def boom_news(s, n=None):
            raise RuntimeError("yahoo 429")

        c = mf.check_candidate(cand(), boom_edgar, boom_news)
        self.assertEqual(c["ma_status"], mf.STATUS_UNKNOWN)
        self.assertIn("查詢失敗", c["ma_note"])

    def test_one_source_fails_still_clear_with_note(self):
        def boom_news(s, n=None):
            raise RuntimeError("yahoo 429")

        c = mf.check_candidate(cand(), no_edgar, boom_news)
        self.assertEqual(c["ma_status"], mf.STATUS_CLEAR)
        self.assertIn("可能不完整", c["ma_note"])

    def test_one_source_fails_but_other_finds_target(self):
        def boom_news(s, n=None):
            raise RuntimeError("x")

        edgar = lambda s: [{"source": "edgar", "role": mf.ROLE_TARGET, "date": "2026-10-01", "text": "SC TO-T", "url": None}]
        c = mf.check_candidate(cand(), edgar, boom_news)
        self.assertEqual(c["ma_status"], mf.STATUS_EXCLUDED)

    def test_edgar_has_no_such_ticker_noted(self):
        c = mf.check_candidate(cand(), lambda s: None, no_news)
        self.assertEqual(c["ma_status"], mf.STATUS_CLEAR)
        self.assertIn("EDGAR 查無此代碼", c["ma_note"])


class NewsParsingTests(unittest.TestCase):
    def test_new_format(self):
        item = {"content": {"title": "T", "pubDate": "2026-10-01T12:00:00Z", "canonicalUrl": {"url": "http://x"}}}
        title, dt, url = mf._parse_news_item(item)
        self.assertEqual((title, url), ("T", "http://x"))
        self.assertEqual(dt.year, 2026)

    def test_old_format(self):
        ts = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
        title, dt, url = mf._parse_news_item({"title": "T2", "providerPublishTime": ts, "link": "http://y"})
        self.assertEqual((title, url), ("T2", "http://y"))
        self.assertEqual(dt.month, 10)

    def test_fetch_news_respects_lookback(self):
        from unittest import mock

        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        new = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        fake_news = [
            {"content": {"title": "Abcd Therapeutics to be acquired by BigPharma", "pubDate": old}},
            {"content": {"title": "Abcd Therapeutics to be acquired by OtherCo", "pubDate": new}},
        ]

        class FakeTicker:
            news = fake_news

        with mock.patch("yfinance.Ticker", return_value=FakeTicker()):
            out = mf.fetch_news_evidence("ABCD", "Abcd Therapeutics Inc.", lookback_days=90)
        self.assertEqual(len(out), 1)
        self.assertIn("OtherCo", out[0]["text"])


class EdgarParsingTests(unittest.TestCase):
    def test_filters_by_lookback_and_form(self):
        from unittest import mock

        today = datetime.now(timezone.utc).date()
        recent_d = (today - timedelta(days=10)).isoformat()
        old_d = (today - timedelta(days=400)).isoformat()
        submissions = {
            "filings": {"recent": {
                "form": ["SC 14D9", "10-Q", "SC TO-T", "8-K"],
                "filingDate": [recent_d, recent_d, old_d, recent_d],
                "items": ["", "", "", "5.01"],
                "accessionNumber": ["0000000001-26-000001", "0000000001-26-000002", "0000000001-26-000003", "0000000001-26-000004"],
                "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
            }}
        }
        with mock.patch.object(mf, "_cik_for", return_value=1234), \
             mock.patch.object(mf, "_sec_get", return_value=submissions):
            out = mf.fetch_edgar_evidence("ABCD", lookback_days=90)
        forms = [e["text"] for e in out]
        self.assertEqual(len(out), 2)  # SC 14D9 與 8-K 5.01；舊的 SC TO-T 與 10-Q 不算
        self.assertTrue(any("SC 14D9" in f for f in forms))
        self.assertTrue(any("5.01" in f for f in forms))
        self.assertTrue(out[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/1234/"))

    def test_unknown_ticker_returns_none(self):
        from unittest import mock

        with mock.patch.object(mf, "_cik_for", return_value=None):
            self.assertIsNone(mf.fetch_edgar_evidence("ZZZZ"))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        storage.save("candidates", {"candidates": []})
        main._job_state.update(running=False, phase=None)

    def test_screen_candidates_splits(self):
        cs = [cand("AAA"), cand("BBB"), cand("CCC")]

        def edgar(sym):
            if sym == "BBB":
                return [{"source": "edgar", "role": mf.ROLE_TARGET, "date": "2026-10-01", "text": "SC 14D9", "url": None}]
            if sym == "CCC":
                return [{"source": "edgar", "role": mf.ROLE_ACQUIRER, "date": "2026-10-01", "text": "S-4", "url": None}]
            return []

        kept, excluded = mf.screen_candidates(cs, edgar_fetch=edgar, news_fetch=no_news)
        self.assertEqual([c["symbol"] for c in kept], ["AAA", "CCC"])
        self.assertEqual([c["symbol"] for c in excluded], ["BBB"])

    def test_apply_ma_filter_updates_result_and_progress(self):
        from unittest import mock

        result = {"candidates": [cand("AAA"), cand("BBB")], "candidate_count": 2}

        def fake_screen(cs, progress=None, **kw):
            for i, c in enumerate(cs, 1):
                progress(i, len(cs), c["symbol"])
            return [cs[0]], [cs[1]]

        with mock.patch.object(ma_filter_module(), "screen_candidates", side_effect=fake_screen):
            main._apply_ma_filter(result)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["ma_excluded_count"], 1)
        self.assertEqual(result["excluded_ma"][0]["symbol"], "BBB")
        self.assertEqual(main._job_state["phase"], "ma")

    def test_disabled_flag_skips_filter(self):
        from unittest import mock

        result = {"candidates": [cand("AAA")], "candidate_count": 1}
        with mock.patch.object(ma_filter_module(), "ENABLED", False):
            main._apply_ma_filter(result)
        self.assertNotIn("excluded_ma", result)

    def test_restore_endpoint_moves_back_and_flags(self):
        excluded = cand("BBB")
        excluded.update(ma_status=mf.STATUS_EXCLUDED, ma_role=mf.ROLE_UNCLEAR)
        storage.save("candidates", {
            "candidates": [cand("AAA")], "excluded_ma": [excluded],
            "candidate_count": 1, "ma_excluded_count": 1,
        })
        data = main.restore_excluded_candidate("bbb")
        self.assertEqual([c["symbol"] for c in data["candidates"]], ["AAA", "BBB"])
        self.assertEqual(data["excluded_ma"], [])
        self.assertEqual(data["candidate_count"], 2)
        restored = data["candidates"][1]
        self.assertEqual(restored["ma_status"], mf.STATUS_REVIEW)
        self.assertTrue(restored["ma_manually_restored"])
        self.assertEqual(storage.load("candidates")["ma_excluded_count"], 0)

    def test_restore_unknown_symbol_404(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as cm:
            main.restore_excluded_candidate("NOPE")
        self.assertEqual(cm.exception.status_code, 404)


def ma_filter_module():
    return main.ma_filter


if __name__ == "__main__":
    unittest.main()
