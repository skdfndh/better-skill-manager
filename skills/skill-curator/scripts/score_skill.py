"""计算v2分数和填报完整性；不核实事实、不推导动作、不改写文件。"""

import argparse
import json
import re
from pathlib import Path


RUBRICS = {
    "quality": {"trigger": 20, "process": 20, "resources": 15, "boundaries": 15, "compatibility": 15, "upkeep": 15},
    "global_value": {"current_need": 30, "unique_value": 30, "independent_entry": 20, "cross_context": 20},
    "distillation": {"reusable_core": 30, "separability": 25, "recipient": 25, "verifiability": 20},
}
RETIREMENT_KEYS = ("target_version", "value_preserved", "replacement_verified", "dependencies_resolved", "recovery_ready")


def checked_mapping(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(label + " 必须是对象")
    if set(value) - set(allowed):
        raise ValueError(label + " 包含未支持的字段")
    return value


def score_group(value, weights):
    value = checked_mapping(value, weights, "评分维度")
    points = 0
    assessed_weight = 0
    missing = []
    for key, weight in weights.items():
        item = value.get(key)
        if item is None:
            missing.append(key)
            continue
        item = checked_mapping(item, {"rating", "evidence"}, key)
        rating = item.get("rating")
        if rating is None:
            missing.append(key)
            continue
        if type(rating) is not int or not 0 <= rating <= 4:
            raise ValueError(key + " 档位须为0至4的整数或null")
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            raise ValueError(key + " 已评分项必须附证据")
        points += weight * rating / 4
        assessed_weight += weight
    return {"score": points if not missing else None, "range": [points, points + 100 - assessed_weight], "assessed_weight": assessed_weight, "unknown": missing}


def calculate(record):
    allowed = {"rubric_version", "skill", "entry_sha256", "package_sha256", "retirement_checks", *RUBRICS}
    checked_mapping(record, allowed, "评价输入")
    if type(record.get("rubric_version")) is not int or record["rubric_version"] != 2:
        raise ValueError("只接受rubric_version为2的评价，旧分数应保留原口径")
    if "skill" in record and (not isinstance(record["skill"], str) or not record["skill"].strip()):
        raise ValueError("skill必须为非空名称")
    for key in ("entry_sha256", "package_sha256"):
        if record.get(key) is not None and (not isinstance(record[key], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", record[key])):
            raise ValueError(key + " 须为SHA-256或null")
    scores = {name: score_group(record.get(name, {}), weights) for name, weights in RUBRICS.items()}
    checks = checked_mapping(record.get("retirement_checks", {}), RETIREMENT_KEYS, "回收门槛")
    states = {}
    for key in RETIREMENT_KEYS:
        item = checks.get(key)
        if item is None:
            states[key] = "unknown"
            continue
        item = checked_mapping(item, {"state", "evidence"}, key)
        state = item.get("state", "unknown")
        if not isinstance(state, str) or state not in {"pass", "fail", "unknown"}:
            raise ValueError(key + " 状态须为pass、fail或unknown")
        if state != "unknown" and (not isinstance(item.get("evidence"), str) or not item["evidence"].strip()):
            raise ValueError(key + " 已判定门槛必须附证据")
        states[key] = state
    return {
        "rubric_version": 2,
        "skill": record.get("skill"),
        "entry_sha256": record.get("entry_sha256"),
        "package_sha256": record.get("package_sha256"),
        "scores": scores,
        "retirement": {"passed": list(states.values()).count("pass"), "total": 5, "states": states, "reported_complete": all(state == "pass" for state in states.values())},
        "authorizes_execution": False,
        "scope": "只计算填报分数和门槛数量，未核实证据，不产生删除权限",
    }


def main():
    parser = argparse.ArgumentParser(description="只读计算Q/G/D区间与R填报门槛")
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.input.read_text(encoding="utf-8-sig"))
        result = calculate(record)
    except (OSError, ValueError) as exc:
        parser.exit(2, "评价输入无效：" + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
