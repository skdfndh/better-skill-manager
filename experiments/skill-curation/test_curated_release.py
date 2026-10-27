"""仅检查公开入口的引用契约及中文简介，不扫描或修改用户技能库。"""

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("curated_release_server", REPO / "server.py")
SERVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)


class ChineseSummaryTests(unittest.TestCase):
    def test_short_chinese_overrides_generic_name(self):
        for name, description in [
            ("writing-workflow", "根据语言与目标选择中文润色或中英文去模板表达专长。"),
            ("pdf", "只在本地提取扫描文档文字。"),
            ("find-skills", "维护本次任务选定的技能集合。"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(SERVER.summarize_skill_zh(name, description), description)

    def test_length_boundary_and_outer_whitespace(self):
        description = "中" * 35
        self.assertEqual(SERVER.summarize_skill_zh("pdf", description), description)
        self.assertEqual(SERVER.summarize_skill_zh("pdf", "  简短中文用途  "), "简短中文用途")
        self.assertEqual(SERVER.summarize_skill_zh("pdf", "中" * 36), "用于读取、处理或生成 PDF 文档。")

    def test_existing_fallbacks_remain_available(self):
        self.assertEqual(SERVER.summarize_skill_zh("pdf", "Read PDF documents"), "用于读取、处理或生成 PDF 文档。")
        self.assertEqual(SERVER.summarize_skill_zh("find-skills", "Discover agent skills"), "用于根据任务查找、筛选和安装合适的 AI 技能。")
        self.assertTrue(SERVER.summarize_skill_zh("unknown-tool", ""))


class EntryDependencyTests(unittest.TestCase):
    def test_local_references_or_declared_runtime_dependencies(self):
        definitions = json.loads((REPO / "skills/runtime-dependencies.json").read_text(encoding="utf-8"))
        root = (REPO / "skills").resolve()
        for name, dependencies in definitions.items():
            for path in (root / name).rglob("*.md"):
                for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                    if target.startswith(("https:", "http:")):
                        continue
                    resolved = (path.parent / target.split("#", 1)[0]).resolve()
                    with self.subTest(entry=name, reference=target):
                        self.assertTrue(resolved.is_relative_to(root), "引用离开了技能源码目录")
                        if not resolved.is_file():
                            relative = resolved.relative_to(root)
                            self.assertEqual(relative.parts[-1], "SKILL.md")
                            self.assertEqual(len(relative.parts), 2)
                            self.assertIn(relative.parts[0], dependencies, "缺失引用没有声明为外部运行依赖")


if __name__ == "__main__":
    unittest.main()
