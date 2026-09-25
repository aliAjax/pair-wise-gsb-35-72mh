import math
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class GeodeticFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.epoch = seed_demo(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_adjust_review_publish_and_compare(self):
        result = self.db.adjust(self.epoch, "alice", "editor")
        self.assertLess(result["residual_rms"], 0.02)
        self.db.transition(self.epoch, "alice", "editor", "submit")
        approved = self.db.transition(self.epoch, "bob", "reviewer", "approve")
        self.assertEqual(approved["status"], "approved")
        published = self.db.transition(self.epoch, "bob", "reviewer", "publish")
        self.assertEqual(published["status"], "published")
        self.assertTrue(self.db.results(self.epoch))
        self.assertTrue(any(item["action"] == "epoch.publish" for item in self.db.audit(self.epoch)))

    def test_duplicate_and_role_conflict_are_rejected(self):
        with self.assertRaisesRegex(DomainError, "疑似重复"):
            self.db.add_observation(self.epoch, "alice", "distance", "A", "B", 100.0)
        with self.assertRaises(DomainError) as cm:
            self.db.adjust(self.epoch, "bob", "viewer")
        self.assertEqual(cm.exception.status, 403)


class OutlierConsoleTest(unittest.TestCase):
    """异常复核台：开单、复测、排除重算、提交门禁和审核视图。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.epoch = seed_demo(self.db)
        # 13 条与示例一致的 A-C 边长提供多余观测，再加 1 条含 1m 粗差的边长。
        exact = math.hypot(50.0, 50.0)
        for _ in range(13):
            self.db.add_observation(self.epoch, "alice", "distance", "A", "C", exact, allow_duplicate=True)
        self.bad = self.db.add_observation(self.epoch, "alice", "distance", "A", "C", exact + 1.0, allow_duplicate=True)["id"]
        self.db.adjust(self.epoch, "alice", "editor")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self):
        cases = self.db.list_outlier_cases(self.epoch)
        self.assertEqual(len(cases), 1)
        return cases[0]

    def test_adjust_opens_case_with_residual_limit_and_reason(self):
        case = self._case()
        self.assertEqual(case["observation_id"], self.bad)
        self.assertEqual(case["status"], "pending")
        self.assertGreater(abs(case["std_residual"]), case["limit_value"])
        self.assertIn("限差", case["reason"])
        self.assertEqual(case["kind"], "distance")

    def test_submit_blocked_until_outlier_handled(self):
        with self.assertRaisesRegex(DomainError, "未处理的异常观测"):
            self.db.transition(self.epoch, "alice", "editor", "submit")
        case = self._case()
        self.db.remeasure_outlier(self.epoch, case["id"], "alice", "安排本周复测")
        epoch = self.db.transition(self.epoch, "alice", "editor", "submit")
        self.assertEqual(epoch["status"], "review")

    def test_remeasure_keeps_observation_adopted(self):
        case = self._case()
        updated = self.db.remeasure_outlier(self.epoch, case["id"], "alice", "")
        self.assertEqual(updated["status"], "remeasure")
        self.assertTrue(updated["decision_note"])
        summary = self.db.review_summary(self.epoch)
        self.assertIn(self.bad, summary["adopted_ids"])
        self.assertEqual(summary["pending_count"], 0)

    def test_exclude_requires_note_and_editor_role(self):
        case = self._case()
        with self.assertRaisesRegex(DomainError, "依据"):
            self.db.exclude_outlier(self.epoch, case["id"], "alice", "  ")
        with self.assertRaises(DomainError) as cm:
            self.db.remeasure_outlier(self.epoch, case["id"], "alice", role="viewer")
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(DomainError) as cm:
            self.db.exclude_outlier(self.epoch, case["id"], "bob", "依据", role="reviewer")
        self.assertEqual(cm.exception.status, 403)

    def test_exclude_recomputes_and_archives_observation(self):
        case = self._case()
        out = self.db.exclude_outlier(self.epoch, case["id"], "alice", "与14条重复边不符，判定粗差")
        obs = {o["id"]: o for o in self.db.list_observations(self.epoch)}[self.bad]
        self.assertEqual(obs["status"], "excluded")  # 原观测留档
        self.assertIsNotNone(obs["residual"])
        self.assertLess(out["adjustment"]["residual_rms"], 0.01)
        self.assertEqual(out["case"]["status"], "excluded")
        self.assertEqual(out["case"]["decision_note"], "与14条重复边不符，判定粗差")
        self.assertTrue(out["changes"]["shifts"])
        self.assertNotIn(self.bad, out["changes"]["adopted_ids"])
        epoch = self.db.transition(self.epoch, "alice", "editor", "submit")
        self.assertEqual(epoch["status"], "review")

    def test_review_summary_and_records_survive_reject(self):
        case = self._case()
        self.db.exclude_outlier(self.epoch, case["id"], "alice", "粗差，排除")
        self.db.transition(self.epoch, "alice", "editor", "submit")
        summary = self.db.review_summary(self.epoch)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["cases"][0]["decision_note"], "粗差，排除")
        self.assertTrue(summary["cases"][0]["changes"]["shifts"])
        self.assertNotIn(self.bad, summary["adopted_ids"])
        self.db.transition(self.epoch, "bob", "reviewer", "reject")
        summary = self.db.review_summary(self.epoch)
        self.assertEqual(summary["epoch"]["status"], "draft")
        self.assertEqual(len(summary["cases"]), 1)  # 退回后记录仍在
        self.assertEqual(summary["cases"][0]["status"], "excluded")
        obs = {o["id"]: o for o in self.db.list_observations(self.epoch)}[self.bad]
        self.assertEqual(obs["status"], "excluded")


class FailedRecomputeTest(unittest.TestCase):
    """重算失败：沿用上次成果并返回拒因，排除动作整体回滚。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _open_case(self, epoch, obs_id):
        with self.db.connect() as conn:
            conn.execute("UPDATE observations SET status='outlier' WHERE id=?", (obs_id,))
            conn.execute(
                """INSERT INTO outlier_cases(epoch_id,observation_id,residual,std_residual,limit_value,sigma0,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (epoch, obs_id, 0.25, 4.0, 1.0, 0.35, "测试构造的异常", "2026-09-25T00:00:00+00:00"),
            )
        return self.db.list_outlier_cases(epoch)[0]

    def test_insufficient_observations_rolls_back(self):
        epoch = self.db.create_epoch("高程网", "alice")
        self.db.set_point(epoch, "alice", "A", 0, 0, 100, True, True, True)
        self.db.set_point(epoch, "alice", "B", 0, 0, 105, True, True, False)
        self.db.add_observation(epoch, "alice", "height_difference", "A", "B", 5.0)
        self.db.add_observation(epoch, "alice", "height_difference", "A", "B", 5.5, allow_duplicate=True)
        self.db.adjust(epoch, "alice", "editor")
        before = self.db.results(epoch)
        obs_id = self.db.list_observations(epoch)[0]["id"]
        case = self._open_case(epoch, obs_id)
        with self.assertRaisesRegex(DomainError, "沿用上次成果"):
            self.db.exclude_outlier(epoch, case["id"], "alice", "重复观测，保留另一条")
        self.assertEqual(self.db.results(epoch), before)  # 沿用上次成果
        self.assertEqual(self.db.list_observations(epoch)[0]["status"], "outlier")  # 排除回滚
        self.assertEqual(self.db.list_outlier_cases(epoch)[0]["status"], "pending")

    def test_rank_deficiency_rolls_back(self):
        epoch = self.db.create_epoch("单边网", "alice")
        self.db.set_point(epoch, "alice", "A", 0, 0, 100, True, True, True)
        self.db.set_point(epoch, "alice", "B", 100, 0, 100, True, True, True)
        self.db.set_point(epoch, "alice", "C", 50, 1, 100, False, False, True)
        leg = math.hypot(50.0, 1.0)
        self.db.add_observation(epoch, "alice", "distance", "A", "C", leg)
        self.db.add_observation(epoch, "alice", "distance", "B", "C", leg)
        self.db.add_observation(epoch, "alice", "distance", "A", "C", leg, allow_duplicate=True)
        self.db.adjust(epoch, "alice", "editor")
        before = self.db.results(epoch)
        obs_id = [o for o in self.db.list_observations(epoch) if o["p1"] == "B"][0]["id"]
        case = self._open_case(epoch, obs_id)
        with self.assertRaisesRegex(DomainError, "沿用上次成果"):
            self.db.exclude_outlier(epoch, case["id"], "alice", "边长异常")
        self.assertEqual(self.db.results(epoch), before)
        self.assertEqual(self.db.list_outlier_cases(epoch)[0]["status"], "pending")

    def test_pending_case_auto_resolves_when_clean(self):
        epoch = self.db.create_epoch("高程网", "alice")
        self.db.set_point(epoch, "alice", "A", 0, 0, 100, True, True, True)
        self.db.set_point(epoch, "alice", "B", 0, 0, 105, True, True, False)
        self.db.add_observation(epoch, "alice", "height_difference", "A", "B", 5.0)
        self.db.add_observation(epoch, "alice", "height_difference", "A", "B", 5.5, allow_duplicate=True)
        self.db.adjust(epoch, "alice", "editor")
        obs_id = self.db.list_observations(epoch)[0]["id"]
        case = self._open_case(epoch, obs_id)
        self.assertEqual(case["status"], "pending")
        self.db.adjust(epoch, "alice", "editor")  # 重算后该观测不是异常
        self.assertEqual(self.db.list_outlier_cases(epoch)[0]["status"], "resolved")


if __name__ == "__main__":
    unittest.main()
