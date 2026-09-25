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


if __name__ == "__main__":
    unittest.main()
