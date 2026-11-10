"""
每週全市場掃描排程的回歸測試：每週固定時段（預設週一 03:00 台北時間）與 interval 模式。

執行方式（在 bd-app/ 目錄下，需先安裝 backend/requirements.txt 與 httpx）：
    python -m unittest tests/test_weekly_schedule.py -v

測試會把 DATA_DIR 指到暫存資料夾，不會碰到正式 Volume。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="bd-sched-test-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import main  # noqa: E402
import storage  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def write_schedule(obj):
    Path(_TMP, "schedule.json").write_text(json.dumps(obj), encoding="utf-8")


def read_schedule():
    return json.loads(Path(_TMP, "schedule.json").read_text(encoding="utf-8"))


def approx(a, b, seconds=10):
    return abs((a - b).total_seconds()) <= seconds


class ComputeNextRunTests(unittest.TestCase):
    """直接呼叫計算函式（假時鐘）。"""

    def test_redeploy_does_not_push_schedule(self):
        nxt, basis = main.compute_next_scan_run("2026-10-27T03:20:00+00:00", 168, now=dt("2026-10-28T17:37:00"))
        print(f"\n  重新部署：next={nxt.isoformat()} basis={basis}")
        self.assertEqual(nxt, dt("2026-11-03T03:20:00"))
        self.assertNotEqual(nxt, dt("2026-11-04T17:37:00"))
        self.assertEqual(basis, "last_run")

    def test_overdue_catches_up_once_after_5_minutes(self):
        now = dt("2026-10-28T12:00:00")
        nxt, basis = main.compute_next_scan_run("2026-10-01T00:00:00+00:00", 168, now=now)
        print(f"\n  停機超過一週：next={nxt.isoformat()} basis={basis}")
        self.assertEqual(nxt, now + timedelta(minutes=5))
        self.assertEqual(basis, "catchup")

    def test_no_last_run(self):
        now = dt("2026-11-02T10:00:00")
        nxt, basis = main.compute_next_scan_run(None, 168, now=now)
        print(f"\n  沒有 last_run_at：next={nxt.isoformat()} basis={basis}")
        self.assertEqual(nxt, now + timedelta(hours=168))
        self.assertEqual(basis, "initial")

    def test_interval_72h_last_run_2_days_ago(self):
        now = dt("2026-11-02T10:00:00")
        nxt, _ = main.compute_next_scan_run((now - timedelta(days=2)).isoformat(), 72, now=now)
        self.assertEqual(nxt, now + timedelta(days=1))

    def test_legacy_formats(self):
        now = dt("2026-10-28T17:37:00")
        for v in ("2026-10-27T03:20:00Z", "2026-10-27T03:20:00", "2026-10-27T11:20:00+08:00",
                  "2026-10-27T03:20:00.123456+00:00"):
            nxt, _ = main.compute_next_scan_run(v, 168, now=now)
            self.assertEqual(nxt.replace(microsecond=0), dt("2026-11-03T03:20:00"), v)
        nxt, basis = main.compute_next_scan_run("garbage", 168, now=now)
        self.assertEqual(basis, "initial")


class SchedulerIntegrationTests(unittest.TestCase):
    """（interval 模式）真的啟動 APScheduler，檢查實際排定的時間與寫回 schedule.json 的 next_run_at。"""

    @classmethod
    def setUpClass(cls):
        if not main.scheduler.running:
            main.scheduler.start(paused=True)  # paused：只排程不真的觸發掃描

    def tearDown(self):
        if main.scheduler.get_job(main.SCHEDULE_JOB_ID):
            main.scheduler.remove_job(main.SCHEDULE_JOB_ID)

    def _job(self):
        return main.scheduler.get_job(main.SCHEDULE_JOB_ID)

    def test_startup_redeploy_keeps_last_run_basis(self):
        now = datetime.now(UTC)
        last = now - timedelta(days=1, hours=14, minutes=17)
        # 舊版 schedule.json 格式（沒有 next_run_basis 欄位），且 next_run_at 是被往後推的錯誤值
        write_schedule({"enabled": True, "schedule_mode": "interval", "interval_hours": 168, "last_run_at": last.isoformat(),
                        "last_result_count": 12, "next_run_at": (now + timedelta(days=7)).isoformat()})
        main._apply_schedule(storage.load("schedule"))
        job = self._job()
        saved = read_schedule()
        print(f"\n  啟動：job.next_run_time={job.next_run_time.isoformat()} saved={saved['next_run_at']}")
        self.assertTrue(approx(job.next_run_time, last + timedelta(hours=168), 1))
        self.assertEqual(datetime.fromisoformat(saved["next_run_at"]), job.next_run_time)
        self.assertEqual(saved["last_result_count"], 12)
        self.assertTrue(job.coalesce)
        self.assertEqual(job.max_instances, 1)
        self.assertIsInstance(job.trigger, IntervalTrigger)

    def test_startup_overdue_catchup_then_resume_weekly(self):
        write_schedule({"enabled": True, "schedule_mode": "interval", "interval_hours": 168, "last_run_at": "2026-10-01T00:00:00+00:00",
                        "last_result_count": None, "next_run_at": None})
        before = datetime.now(UTC)
        main._apply_schedule(storage.load("schedule"))
        job = self._job()
        print(f"\n  逾期：now={before.isoformat()} next={job.next_run_time.isoformat()} basis={read_schedule()['next_run_basis']}")
        self.assertTrue(approx(job.next_run_time, before + timedelta(minutes=5), 5))
        self.assertEqual(read_schedule()["next_run_basis"], "catchup")
        # 只補一次：IntervalTrigger 從補跑時間之後只會每 168 小時一次
        following = job.trigger.get_next_fire_time(job.next_run_time, job.next_run_time)
        self.assertEqual(following - job.next_run_time, timedelta(hours=168))
        # 補跑完成後以完成時間重新對齊
        self._run_scan(success=True)
        saved = read_schedule()
        last = datetime.fromisoformat(saved["last_run_at"])
        self.assertTrue(approx(datetime.fromisoformat(saved["next_run_at"]), last + timedelta(hours=168), 1))
        self.assertEqual(saved["next_run_basis"], "last_run")
        self.assertTrue(approx(self._job().next_run_time, last + timedelta(hours=168), 1))

    def test_put_schedule_uses_last_run_at(self):
        from fastapi.testclient import TestClient
        now = datetime.now(UTC)
        write_schedule({"enabled": True, "schedule_mode": "interval", "interval_hours": 168,
                        "last_run_at": (now - timedelta(days=2)).isoformat(),
                        "last_result_count": 7, "next_run_at": None})
        client = TestClient(main.app)  # 不用 with，避免觸發 startup 重複啟動排程器
        r = client.put("/api/scan/schedule", json={"enabled": True, "interval_hours": 72})
        self.assertEqual(r.json()["schedule_mode"], "interval")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        nxt = datetime.fromisoformat(body["next_run_at"])
        print(f"\n  PUT 72h：now={now.isoformat()} next={nxt.isoformat()}")
        self.assertTrue(approx(nxt, now + timedelta(days=1), 5))
        self.assertTrue(approx(self._job().next_run_time, now + timedelta(days=1), 5))
        self.assertEqual(body["last_result_count"], 7)
        self.assertEqual(read_schedule()["interval_hours"], 72)
        # 停用
        r = client.put("/api/scan/schedule", json={"enabled": False, "interval_hours": 72})
        self.assertIsNone(r.json()["next_run_at"])
        self.assertIsNone(self._job())

    def test_scan_failure_keeps_last_run_at(self):
        last = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        write_schedule({"enabled": True, "schedule_mode": "interval", "interval_hours": 168, "last_run_at": last,
                        "last_result_count": 5, "next_run_at": None})
        main._apply_schedule(storage.load("schedule"))
        self._run_scan(success=False)
        saved = read_schedule()
        print(f"\n  掃描失敗：last_run_at={saved['last_run_at']} error={main._job_state['error']}")
        self.assertEqual(saved["last_run_at"], last)
        self.assertEqual(saved["last_result_count"], 5)
        self.assertEqual(main._job_state["error"], "模擬 Yahoo 失敗")

    def test_manual_scan_counts_as_run(self):
        last = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        write_schedule({"enabled": True, "schedule_mode": "interval", "interval_hours": 168, "last_run_at": last,
                        "last_result_count": 5, "next_run_at": None})
        main._apply_schedule(storage.load("schedule"))
        self._run_scan(success=True)  # 等同手動按「立即掃描」（mode=nasdaq）
        saved = read_schedule()
        self.assertNotEqual(saved["last_run_at"], last)
        self.assertEqual(saved["last_result_count"], 3)
        new_last = datetime.fromisoformat(saved["last_run_at"])
        self.assertTrue(approx(self._job().next_run_time, new_last + timedelta(hours=168), 1))

    def test_skip_when_job_running(self):
        with main._job_lock:
            main._job_state["running"] = True
        try:
            with mock.patch.object(main, "fetch_nasdaq_universe", return_value=["AAA"]), \
                 mock.patch.object(main, "_run_job") as run_job:
                main._run_full_market_scan_job()
                time.sleep(0.2)
                run_job.assert_not_called()  # 已有工作在跑 → 略過，不另開掃描
        finally:
            with main._job_lock:
                main._job_state["running"] = False

    def _run_scan(self, success: bool):
        fake_result = {"candidates": [], "candidate_count": 3, "universe_size": 100}
        screener = (mock.Mock(return_value=fake_result) if success
                    else mock.Mock(side_effect=RuntimeError("模擬 Yahoo 失敗")))
        with mock.patch.object(main, "run_screener", screener), \
             mock.patch.object(main, "_annotate_pattern_scores", lambda c: None), \
             mock.patch.object(main, "_apply_ma_filter", lambda r: None), \
             mock.patch.object(main, "send_telegram_message", lambda t: True):
            with main._job_lock:
                main._job_state["running"] = True
            main._run_job(["AAA", "BBB"], main.ScreenerConfig(), "nasdaq")
        self.assertFalse(main._job_state["running"])


TPE = "Asia/Taipei"


def tpe(s):
    """台北時間字串 → UTC aware datetime"""
    return datetime.fromisoformat(s).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)


class WeeklyComputeTests(unittest.TestCase):
    """每週固定時段（預設週一 03:00 台北時間）的計算，假時鐘。"""

    def calc(self, last, now):
        return main.compute_next_weekly_scan_run(last, 0, 3, 0, TPE, now=now)

    def test_mid_week_redeploy_keeps_fixed_slot(self):
        # 週一 03:00 開始掃、05:10 完成；週三重新部署
        nxt, basis = self.calc(tpe("2026-11-09T05:10:00").isoformat(), tpe("2026-11-11T14:00:00"))
        print(f"\n  週三重新部署：next={nxt.astimezone(timezone(timedelta(hours=8))).isoformat()} basis={basis}")
        self.assertEqual(nxt, tpe("2026-11-16T03:00:00"))
        self.assertEqual(basis, "weekly")

    def test_missed_slot_catches_up(self):
        # 現況：上次 10-27 03:20 UTC，換成固定時段時已錯過 11-02（週一）03:00
        now = dt("2026-11-02T08:00:00")
        nxt, basis = self.calc("2026-10-27T03:20:00+00:00", now)
        print(f"\n  錯過週一時段：next={nxt.isoformat()} basis={basis}")
        self.assertEqual((nxt, basis), (now + timedelta(minutes=5), "catchup"))

    def test_missed_but_next_slot_soon_waits(self):
        # 週日晚上 20:00 才開機，離週一 03:00 只剩 7 小時 → 不補跑，直接等固定時段
        nxt, basis = self.calc(tpe("2026-10-26T05:00:00").isoformat(), tpe("2026-11-08T20:00:00"))
        self.assertEqual((nxt, basis), (tpe("2026-11-09T03:00:00"), "weekly"))

    def test_restart_during_scan_catches_up(self):
        # 週一 03:00 開始掃，04:00 容器重啟（還沒完成，last_run_at 仍是上週）
        now = tpe("2026-11-09T04:00:00")
        nxt, basis = self.calc(tpe("2026-11-02T05:00:00").isoformat(), now)
        self.assertEqual((nxt, basis), (now + timedelta(minutes=5), "catchup"))

    def test_no_last_run_waits_for_slot(self):
        nxt, basis = self.calc(None, tpe("2026-11-04T10:00:00"))
        self.assertEqual((nxt, basis), (tpe("2026-11-09T03:00:00"), "initial"))

    def test_slot_is_us_market_closed(self):
        # 週一 03:00 台北 = 週日 14:00/15:00 美東，美股休市
        nxt, _ = self.calc(None, tpe("2026-11-04T10:00:00"))
        self.assertEqual(nxt.weekday(), 6)  # UTC 週日
        self.assertEqual(nxt.hour, 19)


class WeeklySchedulerIntegrationTests(unittest.TestCase):
    """每週固定時段：真的啟動 APScheduler（paused），檢查 job 與 schedule.json。"""

    @classmethod
    def setUpClass(cls):
        if not main.scheduler.running:
            main.scheduler.start(paused=True)

    def tearDown(self):
        if main.scheduler.get_job(main.SCHEDULE_JOB_ID):
            main.scheduler.remove_job(main.SCHEDULE_JOB_ID)

    def test_legacy_file_defaults_to_monday_0300_taipei(self):
        last = datetime.now(UTC) - timedelta(hours=1)  # 剛掃完，不會觸發補跑
        write_schedule({"enabled": True, "interval_hours": 168, "last_run_at": last.isoformat(),
                        "last_result_count": 9, "next_run_at": None})   # 舊格式，沒有新欄位
        main._apply_schedule(storage.load("schedule"))
        job = main.scheduler.get_job(main.SCHEDULE_JOB_ID)
        saved = read_schedule()
        local = job.next_run_time.astimezone(timezone(timedelta(hours=8)))
        print(f"\n  舊檔啟動：next(台北)={local.isoformat()} mode={saved['schedule_mode']}")
        self.assertIsInstance(job.trigger, CronTrigger)
        self.assertEqual((local.weekday(), local.hour, local.minute), (0, 3, 0))
        self.assertEqual(saved["schedule_mode"], "weekly")
        self.assertEqual((saved["weekday"], saved["time"], saved["timezone"]), (0, "03:00", TPE))
        self.assertEqual(saved["interval_hours"], 168)
        self.assertEqual(saved["last_result_count"], 9)
        self.assertTrue(job.coalesce)
        self.assertEqual(job.max_instances, 1)

    def test_catchup_then_back_to_fixed_slot(self):
        write_schedule({"enabled": True, "interval_hours": 168, "last_run_at": "2026-10-01T00:00:00+00:00",
                        "last_result_count": None, "next_run_at": None})
        before = datetime.now(UTC)
        main._apply_schedule(storage.load("schedule"))
        job = main.scheduler.get_job(main.SCHEDULE_JOB_ID)
        saved = read_schedule()
        if saved["next_run_basis"] == "catchup":
            self.assertTrue(approx(job.next_run_time, before + timedelta(minutes=5), 5))
            # 補跑之後的下一次回到固定時段（不是補跑時間 + 一週）
            following = job.trigger.get_next_fire_time(job.next_run_time, job.next_run_time)
            local = following.astimezone(timezone(timedelta(hours=8)))
            print(f"\n  補跑後下一次(台北)={local.isoformat()}")
            self.assertEqual((local.weekday(), local.hour, local.minute), (0, 3, 0))
        # 掃描完成後重排：下一個週一 03:00
        SchedulerIntegrationTests._run_scan(self, success=True)
        saved = read_schedule()
        nxt = datetime.fromisoformat(saved["next_run_at"]).astimezone(timezone(timedelta(hours=8)))
        self.assertEqual((nxt.weekday(), nxt.hour, nxt.minute), (0, 3, 0))
        self.assertEqual(saved["next_run_basis"], "weekly")

    def test_put_weekly_and_validation(self):
        from fastapi.testclient import TestClient
        write_schedule({"enabled": False, "interval_hours": 168, "last_run_at": datetime.now(UTC).isoformat(),
                        "last_result_count": 4, "next_run_at": None})
        client = TestClient(main.app)
        got = client.get("/api/scan/schedule").json()
        self.assertEqual((got["schedule_mode"], got["weekday"], got["time"]), ("weekly", 0, "03:00"))
        r = client.put("/api/scan/schedule", json={"enabled": True, "weekday": 0, "time": "3:00"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["time"], "03:00")
        self.assertEqual(body["last_result_count"], 4)
        # 舊版前端只送 enabled + interval_hours：不會把模式改掉
        r = client.put("/api/scan/schedule", json={"enabled": True, "interval_hours": 168})
        self.assertEqual(r.json()["schedule_mode"], "weekly")
        for bad in ({"enabled": True, "weekday": 7}, {"enabled": True, "time": "25:00"},
                    {"enabled": True, "timezone": "Mars/Base"}, {"enabled": True, "schedule_mode": "daily"}):
            self.assertEqual(client.put("/api/scan/schedule", json=bad).status_code, 400, bad)


class MonthlyMomentumUntouchedTests(unittest.TestCase):
    def test_startup_registers_cron_jobs(self):
        write_schedule({"enabled": False, "interval_hours": 168, "last_run_at": None,
                        "last_result_count": None, "next_run_at": None})
        with mock.patch.object(main.scheduler, "start"):
            main.on_startup()
        for job_id in (main.MOMENTUM_JOB_ID, main.DIAMOND_HOLDINGS_JOB_ID):
            self.assertIsInstance(main.scheduler.get_job(job_id).trigger, CronTrigger)
        self.assertIsNone(main.scheduler.get_job(main.SCHEDULE_JOB_ID))


if __name__ == "__main__":
    unittest.main(verbosity=2)
