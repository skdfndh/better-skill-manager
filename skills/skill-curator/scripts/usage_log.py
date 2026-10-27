"""在本机追加技能使用证据，并用成功复盘游标处理晚到反馈。"""

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DIR = Path.home() / ".skill-shelf" / "usage"
CHOICES = {
    "agent": {"codex", "claude-code", "antigravity", "dsh"},
    "role": {"router", "specialist", "support"},
    "outcome": {"delivered", "failed", "cancelled", "unknown"},
    "satisfaction": {"satisfied", "mixed", "dissatisfied", "unknown"},
    "revision": {"none", "minor", "major", "unknown"},
    "source": {"user_explicit", "user_request", "agent_observed"},
    "attribution": {"skill", "routing", "environment", "requirements_change", "execution", "unknown"},
}


def now():
    return datetime.now(timezone.utc).isoformat()


def short_text(value, name, limit=120):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} 必须是 1–{limit} 字符的非空文本")
    return value.strip()


def choice(data, key):
    if not isinstance(data.get(key), str) or data[key] not in CHOICES[key]:
        raise ValueError(f"{key} 必须是 {sorted(CHOICES[key])} 之一")
    return data[key]


class UsageLog:
    def __init__(self, data_dir=DEFAULT_DIR):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "usage.sqlite3", timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS usages (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, agent TEXT NOT NULL,
                skill TEXT NOT NULL, entry TEXT NOT NULL, entry_sha256 TEXT NOT NULL,
                role TEXT NOT NULL, task_kind TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                usage_id TEXT NOT NULL REFERENCES usages(id), kind TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_usage ON events(usage_id, seq);
            CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id = 1), seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO cursor VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY, base_seq INTEGER NOT NULL, high_seq INTEGER NOT NULL,
                summary TEXT NOT NULL, report TEXT, report_sha256 TEXT
            );
        """)

    def close(self):
        self.db.close()

    def record(self, data):
        if not isinstance(data, dict):
            raise ValueError("记录必须是 JSON 对象")
        kind = data.get("kind")
        fields = {
            "start": {"kind", "task", "agent", "entry", "role", "task_kind"},
            "result": {"kind", "usage_id", "event_key", "outcome", "evidence"},
            "feedback": {"kind", "usage_id", "event_key", "satisfaction", "revision", "source", "attribution", "evidence"},
        }
        if not isinstance(kind, str) or kind not in fields or set(data) != fields[kind]:
            raise ValueError("记录类型或字段不正确；请按 usage-tracking.md 填写完整字段")
        if kind == "start":
            return self._start(data)
        usage_id = short_text(data["usage_id"], "usage_id")
        event_key = short_text(data["event_key"], "event_key", 200)
        payload = {"evidence": short_text(data["evidence"], "evidence", 300)}
        for key in ("outcome",) if kind == "result" else ("satisfaction", "revision", "source", "attribution"):
            payload[key] = choice(data, key)
        if kind == "feedback":
            if payload["satisfaction"] != "unknown" and payload["source"] != "user_explicit":
                raise ValueError("满意度只能依据用户明确评价，修改要求本身不能推断满意度")
            if payload["revision"] != "unknown" and payload["source"] == "agent_observed":
                raise ValueError("Agent 自行修改不能记作用户修改要求")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute("SELECT 1 FROM usages WHERE id = ?", (usage_id,)).fetchone():
                raise ValueError("未知 usage_id；不能把反馈猜测关联到最近任务")
            return self._event(event_key, usage_id, kind, payload)

    def _event(self, key, usage_id, kind, payload):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        prior = self.db.execute("SELECT * FROM events WHERE event_key = ?", (key,)).fetchone()
        if prior:
            if (prior["usage_id"], prior["kind"], prior["payload"]) != (usage_id, kind, encoded):
                raise ValueError("event_key 已用于不同内容；不能覆盖历史证据")
            return {"usage_id": usage_id, "seq": prior["seq"], "duplicate": True}
        row = self.db.execute(
            "INSERT INTO events(event_key, usage_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, usage_id, kind, encoded, now()),
        )
        return {"usage_id": usage_id, "seq": row.lastrowid, "duplicate": False}

    def _start(self, data):
        task = short_text(data["task"], "task")
        agent, role = choice(data, "agent"), choice(data, "role")
        task_kind = short_text(data["task_kind"], "task_kind")
        entry = Path(short_text(data["entry"], "entry", 2000)).expanduser().resolve(strict=True)
        if not entry.is_file() or entry.name != "SKILL.md":
            raise ValueError("entry 必须指向实际采用的 SKILL.md 文件")
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        identity = json.dumps([task, agent, str(entry), digest, role], ensure_ascii=False)
        usage_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            prior = self.db.execute("SELECT task_kind FROM usages WHERE id = ?", (usage_id,)).fetchone()
            if prior and prior[0] != task_kind:
                raise ValueError("同一次使用的 task_kind 已存在；不要借重试改写历史")
            self.db.execute("INSERT OR IGNORE INTO usages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (usage_id, task, agent, entry.parent.name, str(entry), digest, role, task_kind, now()))
            return self._event("start:" + usage_id, usage_id, "start", {})

    def recent(self, task=None):
        if task is not None:
            short_text(task, "task")
            rows = self.db.execute("SELECT * FROM usages WHERE task = ? ORDER BY created_at DESC LIMIT 100", (task,))
        else:
            rows = self.db.execute("SELECT * FROM usages ORDER BY created_at DESC LIMIT 30")
        return [dict(row) for row in rows]

    def review(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            base = self.db.execute("SELECT seq FROM cursor WHERE id = 1").fetchone()[0]
            high = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
            rows = self.db.execute("""
                SELECT u.*, e.seq, e.kind, e.payload, e.created_at AS event_at
                FROM usages u JOIN events e ON e.usage_id = u.id
                WHERE u.id IN (SELECT usage_id FROM events WHERE seq > ? AND seq <= ?)
                  AND e.seq <= ? ORDER BY e.seq
            """, (base, high, high)).fetchall()
            usages = {}
            for row in rows:
                if row["id"] not in usages:
                    usages[row["id"]] = {key: row[key] for key in
                        ("id", "task", "agent", "skill", "entry", "entry_sha256", "role", "task_kind", "created_at")}
                    usages[row["id"]].update(new_usage=False, outcome="unknown", satisfaction="unknown",
                        revision="unknown", feedback=[], results=[])
                usage = usages[row["id"]]
                payload = json.loads(row["payload"])
                if row["kind"] == "start":
                    usage["new_usage"] = row["seq"] > base
                elif row["kind"] == "result":
                    usage["outcome"] = payload["outcome"]
                    usage["results"].append({**payload, "seq": row["seq"], "at": row["event_at"]})
                else:
                    for key in ("satisfaction", "revision"):
                        if payload[key] != "unknown":
                            usage[key] = payload[key]
                    usage["feedback"].append({**payload, "seq": row["seq"], "at": row["event_at"],
                                              "new_event": row["seq"] > base})
            report_id = str(uuid.uuid4())
            summary = {"review_id": report_id, "base_seq": base, "high_seq": high,
                       "created_at": now(), "coverage": "仅已记录使用；缺失不等于未使用，满意度未知不等于满意",
                       "new_task_count": len({u["task"] for u in usages.values() if u["new_usage"]}),
                       "affected_usages": list(usages.values()), "authorizes_deletion": False}
            self.db.execute("INSERT INTO reviews(id, base_seq, high_seq, summary) VALUES (?, ?, ?, ?)",
                            (report_id, base, high, json.dumps(summary, ensure_ascii=False)))
        return summary

    def commit_review(self, review_id, report):
        path = Path(report).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".md" or not path.read_text(encoding="utf-8-sig").strip():
            raise ValueError("须先保存非空 Markdown 周报，再确认复盘完成")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            if row is None:
                raise ValueError("未知 review_id")
            if review_id not in path.read_text(encoding="utf-8-sig"):
                raise ValueError("周报须包含对应 review_id，避免提交错误文件")
            if row["report_sha256"]:
                if row["report"] != str(path) or row["report_sha256"] != digest:
                    raise ValueError("已确认的周报文件发生变化，历史摘要不会覆盖")
                return {"committed": True, "duplicate": True, "seq": row["high_seq"]}
            cursor = self.db.execute("SELECT seq FROM cursor WHERE id = 1").fetchone()[0]
            if cursor != row["base_seq"]:
                raise ValueError("复盘游标已改变；请重新生成摘要，不能跳过并发记录")
            self.db.execute("UPDATE cursor SET seq = ? WHERE id = 1", (row["high_seq"],))
            self.db.execute("UPDATE reviews SET report = ?, report_sha256 = ? WHERE id = ?",
                            (str(path), digest, review_id))
        return {"committed": True, "duplicate": False, "seq": row["high_seq"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record")
    record_parser.add_argument("input", type=Path)
    recent_parser = commands.add_parser("recent")
    recent_parser.add_argument("--task")
    commands.add_parser("review")
    commit_parser = commands.add_parser("commit-review")
    commit_parser.add_argument("review_id")
    commit_parser.add_argument("report", type=Path)
    args = parser.parse_args()
    log = None
    try:
        log = UsageLog(args.data_dir)
        if args.command == "record":
            result = log.record(json.loads(args.input.read_text(encoding="utf-8-sig")))
        elif args.command == "recent":
            result = log.recent(args.task)
        elif args.command == "review":
            result = log.review()
        else:
            result = log.commit_review(args.review_id, args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
