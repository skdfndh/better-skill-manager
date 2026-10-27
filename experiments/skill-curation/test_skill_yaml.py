"""解析公开技能元数据，避免将无效 YAML 或错误字段类型发布给 Agent。"""

import re
import unittest
from pathlib import Path

import yaml


SKILLS = Path(__file__).resolve().parents[2] / "skills"


class SkillYamlTests(unittest.TestCase):
    def test_skill_frontmatter(self):
        for path in sorted(SKILLS.glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                content = path.read_text(encoding="utf-8-sig")
                match = re.match(r"^---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
                self.assertIsNotNone(match, "缺少完整的 YAML 元数据")
                data = yaml.safe_load(match.group(1))
                self.assertIsInstance(data, dict)
                self.assertEqual(data.get("name"), path.parent.name)
                description = data.get("description")
                self.assertIsInstance(description, str)
                self.assertTrue(description.strip())
                self.assertLessEqual(len(description), 1024)
                if "metadata" in data:
                    self.assertIsInstance(data["metadata"], dict)

    def test_agent_interface(self):
        for path in sorted(SKILLS.glob("*/agents/openai.yaml")):
            with self.subTest(skill=path.parents[1].name):
                data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
                self.assertIsInstance(data, dict)
                interface = data.get("interface")
                self.assertIsInstance(interface, dict)
                for key in ("display_name", "short_description", "default_prompt"):
                    value = interface.get(key)
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())
                self.assertIn("$" + path.parents[1].name, interface["default_prompt"])


if __name__ == "__main__":
    unittest.main()
