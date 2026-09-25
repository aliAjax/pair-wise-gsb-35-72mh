import math
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo

# 多余观测数：大量同精度重复测回把单位权中误差钉在很小的水平，
# 单条粗差才能稳定越过 3.5σ 限差（后验 σ0 下需要足够冗余度）。
REPEAT_RUNS = 40


def build_level_net(db: Database, bad_value: float = 5.9, clean_value: float = 5.0):
    """K1、K2 为已知水准点，U 为待求点。

    40 个 K1-K2 已知边重复测回提供冗余；U 由两条 K1-U 观测确定，
    其中一条带粗差，平差后两条都会被标记为异常（测回互差超差）。
    """
    epoch = db.create_epoch("高程异常复核网", "alice")
    db.set_point(epoch, "alice", "K1", 0, 0, 100.0, True, True, True)
    db.set_point(epoch, "alice", "K2", 0, 0, 110.0, True, True, True)
    db.set_point(epoch, "alice", "U", 0, 0, 105.0, True, True, False)
    for _ in range(REPEAT_RUNS):
        db.add_observation(epoch, "alice", "height_difference", "K1", "K2", 10.0,
                           allow_duplicate=True)
    clean_id = db.add_observation(epoch, "alice", "height_difference", "K1", "U", clean_value)["id"]
    bad_id = db.add_observation(epoch, "alice", "height_difference", "K1", "U", bad_value,
                                allow_duplicate=True)["id"]
    return epoch, clean_id, bad_id


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


class AnomalyReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.epoch, self.clean, self.bad = build_level_net(self.db)
        self.summary = self.db.adjust(self.epoch, "alice", "editor")

    def tearDown(self):
        self.tmp.cleanup()

    def _bad_anomaly(self):
        for item in self.db.list_anomalies(self.epoch):
            if item["observation"]["id"] == self.bad:
                return item
        self.fail("粗差观测没有生成异常")

    def test_anomaly_is_listed_with_residual_limit_and_reason(self):
        item = self._bad_anomaly()
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["unit"], "m")
        self.assertIsNotNone(item["residual_display"])
        self.assertIsNotNone(item["residual_limit_display"])
        self.assertGreater(abs(item["residual_display"]), item["residual_limit_display"])
        self.assertIn("超过限差", item["reason"])
        self.assertIn("高差 K1-U", item["reason"])
        self.assertIn("σ", item["reason"])
        # 超差观测仍在采用集合里，没有被静默剔除或删除。
        with self.db.connect() as conn:
            adopted = conn.execute(
                "SELECT COUNT(*) AS c FROM observations WHERE status IN ('valid','outlier')"
            ).fetchone()["c"]
        self.assertEqual(adopted, REPEAT_RUNS + 2)

    def test_submit_blocked_until_anomaly_resolved(self):
        with self.assertRaisesRegex(DomainError, "未处理完"):
            self.db.transition(self.epoch, "alice", "editor", "submit")
        # 安排复测但尚未回填，仍属未处理完，依旧不能提交。
        self.db.schedule_remeasure(self.epoch, self._bad_anomaly()["id"], "alice", "editor", "次日复测")
        with self.assertRaisesRegex(DomainError, "未处理完"):
            self.db.transition(self.epoch, "alice", "editor", "submit")

    def test_exclude_with_basis_recomputes_and_keeps_archive(self):
        before_runs = self.db.review_bundle(self.epoch)["run_count"]
        anomaly_id = self._bad_anomaly()["id"]
        outcome = self.db.exclude_observation(self.epoch, anomaly_id, "alice", "editor",
                                              "该测回标尺读数记错，经外业核对后剔除")
        self.assertEqual(outcome["state"], "excluded")
        with self.db.connect() as conn:
            status = conn.execute("SELECT status FROM observations WHERE id=?", (self.bad,)).fetchone()["status"]
        self.assertEqual(status, "excluded")  # 原观测留档而非删除
        record = self.db.get_anomaly(self.epoch, anomaly_id)
        self.assertEqual(record["basis_text"], "该测回标尺读数记错，经外业核对后剔除")
        self.assertIn("excluded", [d["action"] for d in record["decisions"]])
        # 重算产生新批次，审核台能看到前后变化和采用集合。
        bundle = self.db.review_bundle(self.epoch)
        self.assertEqual(bundle["run_count"], before_runs + 1)
        self.assertIsNotNone(bundle["before_after"])
        self.assertEqual(bundle["before_after"]["latest_run_id"], outcome["run_id"])
        self.assertEqual(len(bundle["unresolved"]), 0)
        self.assertTrue(bundle["adopted"])
        # 异常处理完后才允许提交。
        self.db.transition(self.epoch, "alice", "editor", "submit")

    def test_exclude_requires_basis(self):
        with self.assertRaisesRegex(DomainError, "排除依据"):
            self.db.exclude_observation(self.epoch, self._bad_anomaly()["id"],
                                        "alice", "editor", "随意")

    def test_only_editor_in_draft_can_handle_anomaly(self):
        anomaly_id = self._bad_anomaly()["id"]
        with self.assertRaises(DomainError) as cm:
            self.db.exclude_observation(self.epoch, anomaly_id, "alice", "viewer", "x" * 10)
        self.assertEqual(cm.exception.status, 403)
        # 处理完异常后提交进入复核，此时不能再处理任何未决异常。
        # 先造一条新粗差并平差会在 review 被挡，因此直接校验状态闸门。
        self.db.exclude_observation(self.epoch, anomaly_id, "alice", "editor", "x" * 10)
        self.db.transition(self.epoch, "alice", "editor", "submit")
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()
        self.assertEqual(row["state"], "excluded")
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.exclude_observation(self.epoch, anomaly_id, "alice", "editor", "y" * 10)

    def test_remeasure_flow_archives_old_and_adopts_new(self):
        anomaly_id = self._bad_anomaly()["id"]
        self.db.schedule_remeasure(self.epoch, anomaly_id, "alice", "editor", "安排外业复测")
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM observations WHERE id=?", (self.bad,)).fetchone()["status"],
                "superseded",
            )
        outcome = self.db.complete_remeasure(self.epoch, anomaly_id, "alice", "editor", 5.0)
        self.assertEqual(outcome["state"], "remeasured")
        record = self.db.get_anomaly(self.epoch, anomaly_id)
        self.assertEqual(record["new_observation"]["id"], outcome["new_observation_id"])
        self.assertEqual(record["new_observation"]["status"], "valid")
        # 采用集合中的 K1-U 是复测值；旧观测仍留档可查。
        with self.db.connect() as conn:
            old = conn.execute("SELECT * FROM observations WHERE id=?", (self.bad,)).fetchone()
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(len(self.db.list_observations(self.epoch)), REPEAT_RUNS + 3)
        self.assertEqual(len(self.db.review_bundle(self.epoch)["unresolved"]), 0)

    def test_reviewer_reject_keeps_records_for_resubmit(self):
        anomaly_id = self._bad_anomaly()["id"]
        self.db.exclude_observation(self.epoch, anomaly_id, "alice", "editor",
                                    "该测回标尺读数记错，经外业核对后剔除")
        self.db.transition(self.epoch, "alice", "editor", "submit")
        self.db.transition(self.epoch, "bob", "reviewer", "reject")
        # 退回后异常台账、依据、处理经过和原观测全部保留，期次回到草稿。
        record = self.db.get_anomaly(self.epoch, anomaly_id)
        self.assertEqual(record["state"], "excluded")
        self.assertTrue(record["basis_text"])
        self.assertTrue(record["decisions"])
        self.assertEqual(self.db.get_epoch(self.epoch)["status"], "draft")
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM observations WHERE id=?", (self.bad,)).fetchone()["status"],
                "excluded",
            )
        # 记录还在，可重新提交。
        self.db.transition(self.epoch, "alice", "editor", "submit")


class RecomputeRejectionTest(unittest.TestCase):
    """观测不足/网形无解时：沿用上次成果并返回拒因，处理记录保留。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.epoch, self.clean, self.bad = build_level_net(self.db)
        self.summary = self.db.adjust(self.epoch, "alice", "editor")
        self.kept_rms = self.summary["residual_rms"]
        # 模拟其余测回已在复测中停用（原行留档），使下一步重算无足够观测。
        # 只归档与目标粗差无关的行（合格边异常会在随后重算中自行判定）。
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE observations SET status='superseded'
                   WHERE epoch_id=? AND id NOT IN
                       (SELECT observation_id FROM anomalies WHERE epoch_id=?)""",
                (self.epoch, self.epoch),
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_exclude_without_enough_observations_keeps_previous_result(self):
        anomaly_id = next(
            a["id"] for a in self.db.list_anomalies(self.epoch) if a["observation"]["id"] == self.bad
        )
        with self.assertRaises(DomainError) as cm:
            self.db.exclude_observation(self.epoch, anomaly_id, "alice", "editor",
                                        "闭合差超限怀疑粗差，先剔除重算")
        self.assertEqual(cm.exception.status, 422)
        self.assertIn("沿用上次成果", str(cm.exception))
        # 最新批次仍是被拒前那次，结果表没有被清空。
        bundle = self.db.review_bundle(self.epoch)
        self.assertEqual(bundle["latest_run"]["residual_rms"], self.kept_rms)
        self.assertTrue(self.db.results(self.epoch))
        # 异常回到待处理、观测恢复采用；排除依据与拒因均留痕。
        record = self.db.get_anomaly(self.epoch, anomaly_id)
        self.assertEqual(record["state"], "open")
        self.assertTrue(record["basis_text"])
        actions = [d["action"] for d in record["decisions"]]
        self.assertIn("excluded", actions)
        self.assertIn("exclude_rejected", actions)
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM observations WHERE id=?", (self.bad,)).fetchone()["status"],
                "outlier",
            )
        # 仍未处理完，提交继续被挡。
        with self.assertRaisesRegex(DomainError, "未处理完"):
            self.db.transition(self.epoch, "alice", "editor", "submit")

    def test_schedule_remeasure_also_rejects_and_restores(self):
        anomaly_id = next(
            a["id"] for a in self.db.list_anomalies(self.epoch) if a["observation"]["id"] == self.bad
        )
        with self.assertRaises(DomainError) as cm:
            self.db.schedule_remeasure(self.epoch, anomaly_id, "alice", "editor", "复测")
        self.assertEqual(cm.exception.status, 422)
        record = self.db.get_anomaly(self.epoch, anomaly_id)
        self.assertEqual(record["state"], "open")
        self.assertIn("remeasure_schedule_rejected", [d["action"] for d in record["decisions"]])
        self.assertEqual(self.db.review_bundle(self.epoch)["latest_run"]["residual_rms"], self.kept_rms)

    def test_submit_without_any_adjustment_is_rejected(self):
        fresh = self.db.create_epoch("空白期", "alice")
        with self.assertRaisesRegex(DomainError, "尚未完成平差"):
            self.db.transition(fresh, "alice", "editor", "submit")


class JudgmentPureFunctionTest(unittest.TestCase):
    """判定层纯函数：角度残差用角秒、限差与闸门规则。"""

    def test_angle_uses_arcseconds(self):
        from anomaly import to_display, unit, residual_limit, is_outlier
        rad = 1.0 / 3600.0 * math.pi / 180.0  # 1 角秒
        self.assertAlmostEqual(to_display("angle", rad), 1.0, places=9)
        self.assertEqual(unit("angle"), "″")
        self.assertGreater(residual_limit(1.0, 0.01), 0)
        self.assertFalse(is_outlier(0.0, 1.0, 0.01, 0))  # 无多余观测不判异常
        self.assertTrue(is_outlier(1.0, 1.0, 0.01, 5))

    def test_can_submit_gate(self):
        from anomaly import can_submit
        self.assertIn("尚未完成平差", can_submit([], 0))
        self.assertIn("未处理完", can_submit(["open"], 1))
        self.assertIn("未处理完", can_submit(["remeasure_planned"], 1))
        self.assertIsNone(can_submit(["excluded", "remeasured", "cleared"], 1))

    def test_diff_runs_reports_point_changes(self):
        from anomaly import diff_runs
        def run(rid, elev, sigma, sigma_e):
            return {"run_id": rid, "sigma0": sigma, "residual_rms": 0.1,
                    "snapshot": {"points": [{"point": "U", "x": 0.0, "y": 0.0, "elevation": elev,
                                            "sigma_x": 0.0, "sigma_y": 0.0, "sigma_elevation": sigma_e}]}}
        d = diff_runs(run(1, 105.0, 0.2, 0.05), run(2, 105.45, 0.0, 0.0))
        self.assertEqual((d["previous_run_id"], d["latest_run_id"]), (1, 2))
        self.assertAlmostEqual(d["points"][0]["delevation"], 0.45)
        self.assertIsNone(diff_runs(None, run(2, 1.0, 0.0, 0.0)))


if __name__ == "__main__":
    unittest.main()
