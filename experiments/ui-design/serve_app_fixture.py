"""使用合成技能和隔离目录启动真实应用 API，供界面回归验证。"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
baseline = subprocess.check_output(["git", "-C", str(REPO), "show", "1e465ab:index.html"])
WORKSPACE = REPO / "backups" / ("ui-app-qa-" + uuid.uuid4().hex[:8])
WORKSPACE.mkdir(parents=True)
(WORKSPACE / "index-before.html").write_bytes(baseline)
DATA = WORKSPACE / "data"
DATA.mkdir()
LIBRARY = WORKSPACE / "skills"
LIBRARY.mkdir()
TARGETS = [("codex", "Codex"), ("claude-code", "Claude Code"),
           ("antigravity-ide", "Antigravity IDE"), ("dsh", "DSH")]
roots = [{"id": "skill-shelf-library", "label": "统一技能库", "agents": [], "path": str(LIBRARY)}]
targets = []
for key, label in TARGETS:
    folder = WORKSPACE / key
    folder.mkdir()
    roots.append({"id": key, "label": label, "agents": [label], "path": str(folder)})
    targets.append({"id": key, "label": label, "path": str(folder), "link_type": "junction", "preserve_names": []})
config = {"library_path": str(LIBRARY), "auto_link_new_skills": False,
          "roots": roots, "deploy_targets": targets,
          "source_priority": [root["id"] for root in roots]}
(DATA / "roots.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def skill(folder: Path, name: str, description: str) -> None:
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n只处理用户要求的范围，完成后说明验证结果。\n",
        encoding="utf-8",
    )


examples = [
    ("ui-design", "界面设计、布局、交互和可访问性检查的统一入口"),
    ("api-review", "接口开发、代码审查和调试"),
    ("release-helper", "部署服务、检查运维配置和发布流程"),
    ("deck-helper", "制作和整理演示文稿"),
    ("writing-guide", "中文写作与表达改进"),
    ("security-check", "安全检查、权限与代码规范"),
    ("csv-helper", "整理表格与数据转换"),
    ("motion-guide", "前端动效和界面交互实现"),
    ("pdf-helper", "提取PDF文档与文字"),
    ("git-helper", "Git代码管理、提交与分支操作"),
    ("form-patterns", "前端表单设计和可访问性"),
    ("note-helper", "整理本地工作笔记"),
]
for name, description in examples:
    skill(LIBRARY / name, name, description)
shutil.copytree(LIBRARY / "api-review", WORKSPACE / "codex/api-review")
skill(WORKSPACE / "claude-code/form-patterns", "form-patterns", "不同版本的表单布局与校验指南")
skill(WORKSPACE / "dsh/local-helper", "local-helper", "本地创建的实用工具，尚未导入统一库")
with zipfile.ZipFile(WORKSPACE / "sample.skill", "w") as archive:
    archive.writestr("archive-example/SKILL.md", "---\nname: archive-example\ndescription: 压缩包导入的合成示例\n---\n\n# 示例\n")
(DATA / "skills-notes.json").write_text(json.dumps({
    "notes": {"ui-design": "统一入口：按需求选择设计与交互专长。", "api-review": "常用的接口检查工具。"},
    "categories": {"ui-design": "前端与动效"},
}, ensure_ascii=False), encoding="utf-8")
(DATA / "skills-sources.json").write_text(json.dumps({
    "ui-design": {"type": "local"},
    "api-review": {"type": "git", "repository_url": "https://github.com/example/agent-skills", "relative_path": "api-review"},
    "deck-helper": {"type": "archive", "file_name": "office-skills.zip"},
}, ensure_ascii=False), encoding="utf-8")
os.environ["SKILL_SHELF_DATA_DIR"] = str(DATA)
spec = importlib.util.spec_from_file_location("skill_shelf_fixture", REPO / "server.py")
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)
for index, target in enumerate(targets):
    app.create_directory_junction(LIBRARY / "ui-design", Path(target["path"]) / "ui-design")
    if index < 2:
        app.create_directory_junction(LIBRARY / "deck-helper", Path(target["path"]) / "deck-helper")
app.scan()
httpd = app.create_server(port=0, start_watcher=False)
info = {"pid": os.getpid(), "url": f"http://127.0.0.1:{httpd.server_port}",
        "workspace": str(WORKSPACE), "archive": str(WORKSPACE / "sample.skill")}
(REPO / "backups/ui-app-session.json").write_text(json.dumps(info), encoding="utf-8")
print(json.dumps(info), flush=True)
try:
    httpd.serve_forever()
finally:
    httpd.server_close()
