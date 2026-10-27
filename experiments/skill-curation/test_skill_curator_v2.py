"""验证计分、未知项和门槛计数，不执行任何技能清理。"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "skills/skill-curator/scripts/score_skill.py"
SPEC = importlib.util.spec_from_file_location("skill_curator_score_v2", SCRIPT)
SCORER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORER)


def rated_group(name, rating):
    return {key: {"rating": rating, "evidence": "合成测试证据"} for key in SCORER.RUBRICS[name]}


class ScoreV2Tests(unittest.TestCase):
    def test_three_scores_are_separate(self):
        result = SCORER.calculate({"rubric_version": 2, "quality": rated_group("quality", 4), "global_value": rated_group("global_value", 1), "distillation": rated_group("distillation", 3)})
        self.assertEqual([result["scores"][name]["score"] for name in SCORER.RUBRICS], [100, 25, 75])
        self.assertNotIn("overall_score", result)

    def test_unobserved_usage_is_not_zero(self):
        result = SCORER.calculate({"rubric_version": 2, "global_value": {"current_need": {"rating": None, "evidence": "暂无使用记录"}}})
        value = result["scores"]["global_value"]
        self.assertIsNone(value["score"])
        self.assertEqual(value["range"], [0, 100])
        self.assertEqual(value["assessed_weight"], 0)

    def test_partial_weight_is_not_renormalized(self):
        result = SCORER.calculate({"rubric_version": 2, "global_value": {"current_need": {"rating": 3, "evidence": "合成的当前需求证据"}}})
        value = result["scores"]["global_value"]
        self.assertEqual(value["range"], [22.5, 92.5])
        self.assertEqual(value["assessed_weight"], 30)
        self.assertIsNone(value["score"])

    def test_full_scores_do_not_complete_retirement_checks(self):
        result = SCORER.calculate({"rubric_version": 2, **{name: rated_group(name, 4) for name in SCORER.RUBRICS}})
        self.assertEqual(result["retirement"]["passed"], 0)
        self.assertFalse(result["retirement"]["reported_complete"])
        self.assertFalse(result["authorizes_execution"])

    def test_failed_check_cannot_be_compensated(self):
        checks = {key: {"state": "pass", "evidence": "合成的已核对证据"} for key in SCORER.RETIREMENT_KEYS}
        checks["value_preserved"] = {"state": "fail", "evidence": "仍有脚本未迁移"}
        result = SCORER.calculate({"rubric_version": 2, "retirement_checks": checks})
        self.assertEqual(result["retirement"]["passed"], 4)
        self.assertFalse(result["retirement"]["reported_complete"])

    def test_filled_checks_do_not_authorize_execution(self):
        checks = {key: {"state": "pass", "evidence": "合成测试中的填报证据"} for key in SCORER.RETIREMENT_KEYS}
        result = SCORER.calculate({"rubric_version": 2, "retirement_checks": checks})
        self.assertTrue(result["retirement"]["reported_complete"])
        self.assertFalse(result["authorizes_execution"])

    def test_rejects_unsupported_values_and_claimed_authorization(self):
        records = [
            {"rubric_version": 1},
            {"rubric_version": 2.0},
            {"rubric_version": 2, "user_authorized": True},
            {"rubric_version": 2, "quality": {"trigger": {"rating": True, "evidence": "合成"}}},
            {"rubric_version": 2, "quality": {"trigger": {"rating": 5, "evidence": "合成"}}},
            {"rubric_version": 2, "quality": {"trigger": {"rating": 3, "evidence": " "}}},
            {"rubric_version": 2, "quality": {"unknown_dimension": {"rating": 4, "evidence": "合成"}}},
            {"rubric_version": 2, "retirement_checks": {"target_version": {"state": []}}},
            {"rubric_version": 2, "retirement_checks": {"target_version": {"state": "pass"}}},
        ]
        for record in records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                SCORER.calculate(record)

    def test_cli_reads_input_without_rewriting(self):
        with tempfile.TemporaryDirectory(prefix="score-v2-check-") as folder:
            source = Path(folder) / "input.json"
            original = json.dumps({"rubric_version": 2, "skill": "synthetic-example"}).encode()
            source.write_bytes(original)
            result = subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), str(source)], check=True, capture_output=True, text=True, encoding="utf-8")
            self.assertFalse(json.loads(result.stdout)["authorizes_execution"])
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(set(Path(folder).iterdir()), {source})


if __name__ == "__main__":
    unittest.main()
