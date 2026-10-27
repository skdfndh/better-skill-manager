"""检查 UI 整合的真实文件引用、来源漂移、部署联接及路由结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
EXPERIMENT = Path(__file__).resolve().parent
IGNORED = {".git", "__pycache__", "node_modules"}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def package_hash(folder: Path) -> str:
    result = hashlib.sha256()
    files = sorted(
        p for p in folder.rglob("*")
        if p.is_file() and not any(x in IGNORED for x in p.relative_to(folder).parts)
    )
    for path in files:
        result.update(path.relative_to(folder).as_posix().encode())
        result.update(b"\0")
        result.update(hashlib.sha256(path.read_bytes()).digest())
    return result.hexdigest()


def check_files(library: Path, evaluation: dict) -> dict:
    maxima = list(evaluation["rubric"].values())
    assert sum(maxima) == 100, "评分维度权重之和应为100"
    rows = evaluation["skills"]
    names = {row["name"] for row in rows}
    assert len(names) == len(rows), "评估记录中有重复技能"
    for row in rows:
        assert len(row["scores"]) == len(maxima), row["name"]
        assert all(0 <= n <= cap for n, cap in zip(row["scores"], maxima)), row["name"]
        assert sum(row["scores"]) == row["total"], row["name"]
        if row["name"] != "emil-frontend":
            assert package_hash(library / row["name"]) == row["package_sha256"], (
                f"专长内容已变化，应先复评：{row['name']}"
            )

    for name in ("ui-design", "emil-frontend"):
        source = REPO / "skills" / name
        target = library / name
        assert package_hash(source) == package_hash(target), f"源文件与部署副本不同：{name}"
        assert (target / "SKILL.md").is_file(), f"缺少入口：{name}"

    allowed = [(library / name).resolve() for name in names | {"ui-design"}]
    checked = 0
    dependencies = set()
    for name in ("ui-design", "emil-frontend"):
        for path in (library / name).rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if re.match(r"[a-z]+://", link):
                    continue
                resolved = (path.parent / link.split("#")[0]).resolve()
                assert resolved.is_file(), f"引用文件不存在：{name}/{path.name} -> {link}"
                assert any(resolved.is_relative_to(root) for root in allowed), (
                    f"引用超出已声明技能范围：{link}"
                )
                checked += 1
                if name == "ui-design" and resolved.name == "SKILL.md":
                    dependencies.add(resolved.parent.name)
    assert dependencies == names - {"emil-frontend"}, "专长目录覆盖不完整或存在循环入口"
    return {"scored_skills": len(rows), "unchanged_specialists": len(names) - 1,
            "resolved_markdown_links": checked, "specialists": len(dependencies)}


def check_deployment(library: Path, config_path: Path) -> dict:
    config = load_json(config_path)
    targets = {}
    for entry in config.get("deploy_targets", []):
        value = entry["path"].replace("${USERPROFILE}", str(Path.home()))
        folder = Path(os.path.expandvars(os.path.expanduser(value)))
        if folder.resolve() == library.resolve() or not folder.is_dir():
            continue
        targets[str(folder.resolve())] = folder
    count = 0
    for folder in targets.values():
        for name in ("ui-design", "emil-frontend"):
            path = folder / name
            assert path.is_dir(), f"Agent入口缺失：{folder.name}/{name}"
            assert path.resolve() == (library / name).resolve(), f"Agent入口指向错误：{name}"
            assert path.lstat().st_file_attributes & 0x400, f"Agent入口不是联接：{name}"
            count += 1
    return {"agent_roots": len(targets), "verified_junctions": count}


def grade_routes(cases: list[dict], runs: dict, names: set[str]) -> dict:
    summary = {}
    expected_ids = {case["id"] for case in cases}
    for label, run in runs.items():
        answers = {answer["id"]: answer for answer in run["cases"]}
        assert set(answers) == expected_ids and len(run["cases"]) == len(cases), (
            f"路由输出不完整或含重复用例：{label}"
        )
        grades = []
        for case in cases:
            answer = answers[case["id"]]
            chosen = set(answer["skills"])
            failures = []
            if not set(case.get("require_all", [])).issubset(chosen):
                failures.append("缺少任务必需专长")
            if case.get("require_any") and not chosen.intersection(case["require_any"]):
                failures.append("未选择可接受的主专长")
            if chosen.intersection(case.get("forbid", [])):
                failures.append("选择了不适用或未获显式授权的专长")
            if case.get("forbid_all_ui") and chosen:
                failures.append("非UI任务误入UI路径")
            if chosen - names:
                failures.append("选择了目录外专长或递归入口")
            if len(chosen) > case.get("max_skills", 4):
                failures.append("一次加载过多专长")
            if answer["mode"] != case["mode"]:
                failures.append("只读、实施或领域边界判断不符")
            command = str(answer.get("command") or "").split()
            if case.get("command") and case["command"] not in command:
                failures.append("子命令与目标不符")
            grades.append({"id": case["id"], "passed": not failures,
                           "failures": failures, "evidence": answer["reason"]})
        summary[label] = {"passed": sum(g["passed"] for g in grades),
                          "total": len(grades), "grades": grades}
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=Path.home() / ".skill-shelf" / "skills")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    evaluation = load_json(EXPERIMENT / "evaluation.json")
    output = {"files": check_files(args.library, evaluation)}
    if args.config:
        output["deployment"] = check_deployment(args.library, args.config)
    runs_path = EXPERIMENT / "routing-results.json"
    if runs_path.is_file():
        cases = load_json(EXPERIMENT / "routing-cases.json")["cases"]
        names = {row["name"] for row in evaluation["skills"]} - {"emil-frontend"}
        output["routes"] = grade_routes(cases, load_json(runs_path), names)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    integrated = output.get("routes", {}).get("integrated")
    if integrated and integrated["passed"] != integrated["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
