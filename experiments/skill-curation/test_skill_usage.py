"""用隔离日志验证去重、反馈归因和跨周游标，避免写入真实使用数据。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills/skill-curator/scripts/usage_log.py"
SPEC = importlib.util.spec_from_file_location("skill_usage", SCRIPT)
USAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(USAGE)


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.entry = self.root / "example" / "SKILL.md"
        self.entry.parent.mkdir()
        self.entry.write_text("合成技能 v1", encoding="utf-8")
        self.log = USAGE.UsageLog(self.root / "usage")
        self.start = {"kind": "start", "task": "synthetic-task", "agent": "codex",
                      "entry": str(self.entry), "role": "specialist", "task_kind": "合成验证"}
        self.id = self.log.record(self.start)["usage_id"]

    def tearDown(self):
        self.log.close()
        self.temp.cleanup()

    def feedback(self, **changes):
        return {"kind": "feedback", "usage_id": self.id, "event_key": "feedback-1",
                "satisfaction": "unknown", "revision": "major", "source": "user_request",
                "attribution": "requirements_change", "evidence": "合成案例：用户新增需求后要求大改", **changes}

    def finish_review(self, review):
        path = self.root / (review["review_id"] + ".md")
        path.write_text("合成周报\n" + review["review_id"], encoding="utf-8")
        return self.log.commit_review(review["review_id"], path)

    def test_start_retry_and_different_versions(self):
        self.assertTrue(self.log.record(self.start)["duplicate"])
        self.entry.write_text("合成技能 v2", encoding="utf-8")
        second = self.log.record(self.start)
        self.assertNotEqual(second["usage_id"], self.id)
        self.assertEqual(len(self.log.recent()), 2)

    def test_silence_and_major_revision_are_not_dissatisfaction(self):
        self.assertEqual(self.log.review()["affected_usages"][0]["satisfaction"], "unknown")
        self.log.record(self.feedback())
        usage = self.log.review()["affected_usages"][0]
        self.assertEqual((usage["satisfaction"], usage["revision"]), ("unknown", "major"))
        self.assertEqual(usage["feedback"][0]["attribution"], "requirements_change")

    def test_only_explicit_user_feedback_can_rate_satisfaction(self):
        with self.assertRaises(ValueError):
            self.log.record(self.feedback(satisfaction="dissatisfied"))
        with self.assertRaises(ValueError):
            self.log.record(self.feedback(source="agent_observed"))
        self.log.record(self.feedback(source="user_explicit", satisfaction="mixed"))
        self.assertEqual(self.log.review()["affected_usages"][0]["satisfaction"], "mixed")

    def test_event_retry_is_idempotent_and_cannot_overwrite(self):
        first = self.log.record(self.feedback())
        self.assertEqual(self.log.record(self.feedback())["seq"], first["seq"])
        with self.assertRaises(ValueError):
            self.log.record(self.feedback(revision="minor"))
        with self.assertRaises(ValueError):
            self.log.record(self.feedback(usage_id="unknown"))

    def test_late_feedback_is_in_next_review_without_new_usage(self):
        first = self.log.review()
        self.finish_review(first)
        self.log.record(self.feedback())
        second = self.log.review()
        self.assertEqual(second["new_task_count"], 0)
        self.assertFalse(second["affected_usages"][0]["new_usage"])
        self.assertTrue(second["affected_usages"][0]["feedback"][0]["new_event"])
        self.assertFalse(second["authorizes_deletion"])

    def test_uncommitted_review_does_not_consume_events(self):
        first, second = self.log.review(), self.log.review()
        self.assertEqual((first["base_seq"], first["high_seq"]), (second["base_seq"], second["high_seq"]))
        self.finish_review(first)
        with self.assertRaises(ValueError):
            self.finish_review(second)

    def test_events_during_review_are_not_skipped(self):
        first = self.log.review()
        self.log.record(self.feedback())
        self.finish_review(first)
        second = self.log.review()
        self.assertGreater(second["high_seq"], first["high_seq"])
        self.assertEqual(second["base_seq"], first["high_seq"])

    def test_report_required_and_commit_retry(self):
        review = self.log.review()
        report = self.root / "wrong.md"
        report.write_text("其他报告", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.log.commit_review(review["review_id"], report)
        self.finish_review(review)
        self.assertTrue(self.finish_review(review)["duplicate"])
        self.assertEqual(self.log.review()["affected_usages"], [])

    def test_result_is_independent_from_user_rating_and_routing_not_double_counted(self):
        self.log.record({**self.start, "role": "router"})
        self.log.record({"kind": "result", "usage_id": self.id, "event_key": "result-1",
                         "outcome": "delivered", "evidence": "合成产物已交付"})
        review = self.log.review()
        self.assertEqual(review["new_task_count"], 1)
        self.assertEqual(len(review["affected_usages"]), 2)
        self.assertEqual(review["affected_usages"][0]["satisfaction"], "unknown")

    def test_rejects_unknown_fields_and_invalid_values(self):
        for changes in ({"user_authorized": True}, {"kind": []}, {"source": []}, {"evidence": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.log.record(self.feedback(**changes))

    def test_cli_with_explicit_isolated_directory(self):
        data = self.root / "input.json"
        data.write_text(json.dumps(self.start, ensure_ascii=False), encoding="utf-8-sig")
        result = subprocess.run([sys.executable, str(SCRIPT), "--data-dir", str(self.root / "cli"),
                                 "record", str(data)], capture_output=True, text=True, encoding="utf-8",
                                env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["usage_id"], self.id)


if __name__ == "__main__":
    unittest.main()
