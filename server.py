"""本地 Skill 目录扫描与去重 API。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BASE_DIR = RESOURCE_DIR
IS_FROZEN = bool(getattr(sys, "frozen", False))
_default_data_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "SkillShelf" if IS_FROZEN else BASE_DIR
DATA_DIR = Path(os.environ.get("SKILL_SHELF_DATA_DIR", str(_default_data_dir))).resolve()
CONFIG_PATH = DATA_DIR / "roots.json"
CACHE_PATH = DATA_DIR / ".scan-cache.json"
REPORT_PATH = DATA_DIR / "latest-scan.json"
IMPORT_PLAN_PATH = DATA_DIR / "latest-import-plan.json"
IMPORT_RESULT_PATH = DATA_DIR / "latest-import-result.json"
DEPLOY_PLAN_PATH = DATA_DIR / "latest-deploy-plan.json"
DEPLOY_RESULT_PATH = DATA_DIR / "latest-deploy-result.json"
DEPLOY_STATUS_PATH = DATA_DIR / "latest-deploy-status.json"
RECONCILE_RESULT_PATH = DATA_DIR / "latest-reconcile-result.json"
CONFLICT_RESULT_PATH = DATA_DIR / "latest-conflict-resolution-result.json"
MANAGED_LINKS_PATH = DATA_DIR / ".managed-links.json"
DELETE_RESULT_PATH = DATA_DIR / "latest-delete-result.json"
NOTES_PATH = DATA_DIR / "skills-notes.json"
SOURCES_PATH = DATA_DIR / "skills-sources.json"
STAGING_DIR = DATA_DIR / ".staging"
HOST = "127.0.0.1"
PORT = 8765
CACHE_VERSION = 1
TEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".py", ".js", ".ts", ".sh", ".ps1", ".html", ".css", ".xml", ".csv"}
IGNORED_DIRS = {".git", "node_modules", "__pycache__"}
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
PREVIEW_TTL_SECONDS = 3600
SOURCE_PREVIEWS: dict[str, dict] = {}
SCAN_LOCK = threading.Lock()


def expand_config_path(value: str) -> Path:
    configured_path = value.replace("${USERPROFILE}", os.environ.get("USERPROFILE", str(Path.home())))
    return Path(os.path.expandvars(os.path.expanduser(configured_path)))


def initialize_runtime_data() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        bundled_config = RESOURCE_DIR / "roots.json"
        if bundled_config.is_file() and IS_FROZEN:
            shutil.copy2(bundled_config, CONFIG_PATH)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 60 * 60
    for path in STAGING_DIR.iterdir():
        if path.is_dir() and path.name.startswith(("source-", "update-", "association-")) and path.stat().st_mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


def read_settings() -> dict:
    config_path = CONFIG_PATH if CONFIG_PATH.is_file() else RESOURCE_DIR / "roots.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    roots = []
    for item in data.get("roots", []):
        path = expand_config_path(item["path"])
        roots.append({**item, "path": path})
    return {
        "library_path": expand_config_path(data.get("library_path", "${USERPROFILE}/.skill-shelf/skills")),
        "auto_link_new_skills": bool(data.get("auto_link_new_skills", True)),
        "auto_link_poll_seconds": max(2, int(data.get("auto_link_poll_seconds", 5))),
        "source_priority": data.get("source_priority", [item["id"] for item in roots]),
        "conflict_resolutions": data.get("conflict_resolutions", {}),
        "deploy_targets": [
            {**item, "path": expand_config_path(item["path"])}
            for item in data.get("deploy_targets", [])
        ],
        "roots": roots,
    }


def read_config() -> list[dict]:
    return read_settings()["roots"]


def inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def skill_dirs(root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    if not root.is_dir():
        return found
    if (root / "SKILL.md").is_file():
        found.append((root, root.name))
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return found
    for child in children:
        if child.name.startswith(".") or child.name.casefold() in {"node_modules", "__pycache__"}:
            continue
        if child.is_dir() and (child / "SKILL.md").is_file():
            found.append((child, child.name))
        elif child.is_file() and child.suffix.casefold() == ".md" and child.name.casefold() != "skill.md":
            found.append((child, child.stem))
    return found


def read_manifest(skill_path: Path, fallback_name: str) -> tuple[str, str]:
    manifest = skill_path if skill_path.is_file() else skill_path / "SKILL.md"
    try:
        text = manifest.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return fallback_name, ""
    match = re.match(r"\s*---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        return fallback_name, ""
    lines = match.group(1).splitlines()
    values: dict[str, str] = {}
    index = 0
    while index < len(lines):
        field = re.match(r"^(name|description)\s*:\s*(.*)$", lines[index])
        if not field:
            index += 1
            continue
        key, value = field.groups()
        value = value.strip()
        if key == "description" and value in {">", ">-", ">+", "|", "|-", "|+"}:
            folded = value.startswith(">")
            block = []
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                block.append(lines[index].strip())
                index += 1
            value = "\n".join(block).strip()
            if folded:
                value = re.sub(r"(?<!\n)\n(?!\n)", " ", value)
        else:
            index += 1
            if len(value) >= 2 and value[0] == value[-1] and value[0] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            elif len(value) >= 2 and value[0] == value[-1] and value[0] == "'":
                value = value[1:-1].replace("''", "'")
        values[key] = value.strip()
    return values.get("name", fallback_name), values.get("description", "")


def collect_file_rows(resolved: Path) -> list[tuple[Path, str, os.stat_result]]:
    if resolved.is_file():
        return [(resolved, "SKILL.md", resolved.stat())]
    files: list[tuple[Path, str, os.stat_result]] = []
    resolved_text = str(resolved)
    for current, directories, filenames in os.walk(resolved, followlinks=False):
        directories[:] = [
            name for name in directories
            if name.casefold() not in IGNORED_DIRS and not is_reparse_point(Path(current) / name)
        ]
        for filename in filenames:
            candidate_text = os.path.join(current, filename)
            if is_reparse_point(Path(candidate_text)):
                continue
            try:
                stat = os.stat(candidate_text, follow_symlinks=False)
            except OSError:
                continue
            relative = os.path.relpath(candidate_text, resolved_text).replace(os.sep, "/")
            files.append((Path(candidate_text), relative, stat))
    return sorted(files, key=lambda row: row[1].casefold())


def load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("version") == CACHE_VERSION and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def fingerprint(skill_path: Path, allowed_roots: list[Path], cache: dict, next_cache: dict) -> tuple[str, float] | None:
    resolved = skill_path.resolve()
    if not any(inside(resolved, root) for root in allowed_roots):
        return None
    manifest = resolved if resolved.is_file() else resolved / "SKILL.md"
    if not manifest.is_file():
        return None
    digest = hashlib.sha256()
    latest_mtime = 0.0
    try:
        files = collect_file_rows(resolved)
        signature_digest = hashlib.sha256()
        file_rows = []
        for file_path, relative, stat in files:
            latest_mtime = max(latest_mtime, stat.st_mtime)
            file_rows.append((file_path, relative))
            signature_digest.update(relative.encode("utf-8", errors="replace"))
            signature_digest.update(b"\0")
            signature_digest.update(str(stat.st_size).encode("ascii"))
            signature_digest.update(b"\0")
            signature_digest.update(str(stat.st_mtime_ns).encode("ascii"))
            signature_digest.update(b"\0")
        signature = signature_digest.hexdigest()
        cache_key = os.path.normcase(str(resolved))
        cached = cache.get(cache_key)
        if cached and cached.get("signature") == signature:
            next_cache[cache_key] = cached
            return cached["content_hash"], cached.get("latest_mtime", latest_mtime)
        for file_path, relative in file_rows:
            data = file_path.read_bytes()
            if file_path.suffix.casefold() in TEXT_SUFFIXES or file_path.name.casefold() == "skill.md":
                data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            digest.update(relative.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
        next_cache[cache_key] = {
            "signature": signature,
            "content_hash": digest.hexdigest(),
            "latest_mtime": latest_mtime,
        }
    except (OSError, ValueError):
        return None
    return next_cache[cache_key]["content_hash"], latest_mtime


def normalized_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


AUTO_CATEGORY_KEYWORDS = {
    "安全与规范": (
        "security", "secure", "privacy", "vulnerability", "malware", "vetter", "compliance",
        "permission", "credential", "secret scanning", "safety", "guardrail", "threat", "authentication", "auth", "login",
        "access control", "隐私", "漏洞", "合规", "权限", "安全审查",
    ),
    "前端与动效": (
        "frontend", "front-end", "front end", "ui", "ux", "user interface", "interface", "css", "html",
        "react", "vue", "svelte", "animate", "animated", "animation", "motion", "gesture", "micro-interaction",
        "website", "web design", "landing page", "dashboard", "component", "figma",
        "design system", "visual design", "prototype", "前端", "界面", "动效", "动画", "交互设计",
    ),
    "系统与运维": (
        "powershell", "shell", "deployment", "deploy", "server", "cloud", "docker", "windows",
        "network", "disk", "filesystem", "file system", "cleanup", "clean up", "backup",
        "monitoring", "ci/cd", "infrastructure", "operations", "maintenance", "运维", "部署",
        "服务器", "磁盘", "清理", "备份", "系统管理",
    ),
    "文档与办公": (
        "document", "documents", "docs", "docx", "word", "pdf", "spreadsheet", "excel", "csv",
        "presentation", "slides", "ppt", "ocr", "diagram", "canvas", "markdown",
        "knowledge base", "office", "文档", "表格", "演示文稿", "幻灯片", "归档", "知识库", "流程图",
    ),
    "AI与写作规范": (
        "artificial intelligence", "large language model", "llm", "agent skill", "skill-creator",
        "find-skills", "skill installer", "install skill", "install skills", "find skill", "discover skill",
        "codex", "claude code", "chatgpt", "prompt", "writing",
        "copywriting", "humanize", "translation", "translate", "language", "chinese", "text",
        "aigc", "content writing", "agent instructions", "ai", "人工智能", "大语言模型", "提示词",
        "写作", "翻译", "中文", "智能体", "技能发现", "技能创建",
    ),
    "开发与调试": (
        "debug", "debugging", "troubleshoot", "coding", "code", "programming", "developer",
        "development", "software", "git", "repository", "refactor", "test-driven", "testing", "tdd",
        "unit test", "api", "python", "javascript", "typescript", "swift", "rust", "java", "cli",
        "build", "compiler", "openspec", "开发", "调试", "编程", "代码", "测试", "版本控制",
    ),
}


def _contains_category_keyword(text: str, keyword: str) -> bool:
    if any(ord(char) > 127 for char in keyword):
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def classify_skill(name: str, description: str) -> str:
    name_text = normalized_name(name)
    description_text = normalized_name(description)
    scores = {}
    for category, keywords in AUTO_CATEGORY_KEYWORDS.items():
        name_matches = 0
        description_matches = 0
        for keyword in keywords:
            if _contains_category_keyword(name_text, keyword):
                name_matches += 1
            if _contains_category_keyword(description_text, keyword):
                description_matches += 1
        score = name_matches * 10 + min(description_matches, 4)
        if score:
            scores[category] = score
    if not scores:
        return "其他工具"
    return max(scores, key=scores.get)


SUMMARY_RULES_ZH = (
    (("hatch-pet", "animated pet", "spritesheet", "sprite sheet", "mascot"), "用于创作和打包动画宠物素材，并检查动画帧与朝向。"),
    (("skill-vetter", "vetter", "malware", "suspicious code"), "用于安装前检查技能中的恶意行为、漏洞和可疑操作。"),
    (("repo-privacy-hardening", "secret scanning", "public repository", "sensitive information"), "用于发布前检查代码仓库中的隐私、密钥和内部信息风险。"),
    (("disk-clean", "disk cleanup", "dry-run cleanup"), "用于核验并清理已确认的临时文件、缓存和构建产物。"),
    (("disk-audit", "disk audit"), "用于检查磁盘或目录内容，评估系统重要性和可清理性。"),
    (("powershell-windows", "powershell", "windows shell"), "用于编写和排查 Windows PowerShell 命令与脚本。"),
    (("git-cloud", "git deployment", "github actions", "cloud server"), "用于通过 Git 和自动化流程把代码部署到云服务器。"),
    (("auth-flow", "authentication flow", "login flow"), "用于设计和实现身份登录与认证流程。"),
    (("openspec", "openspec change"), "用于按 OpenSpec 流程梳理、规划、实施或归档功能变更。"),
    (("skill-curator",), "用于评估技能质量、整理同类技能并整合统一调用入口。"),
    (("skill-creator", "create a new skill", "skill from scratch"), "用于创建、改进和评估 AI 技能及其使用说明。"),
    (("find-skills", "f-d-u-d", "agent skill", "install skills", "discover skills"), "用于根据任务查找、筛选和安装合适的 AI 技能。"),
    (("codex-with-chatgpt", "chatgpt planning", "chatgpt review"), "用于结合 ChatGPT 研究、规划和复核编程工作。"),
    (("project-context-maintainer", "agents.md", "project context"), "用于为项目建立或更新 AI 可读的背景说明与协作规范。"),
    (("grill-with-docs", "domain model", "decision records"), "用于梳理方案与项目术语，并同步记录设计决策。"),
    (("karpathy-guidelines", "coding guidelines", "implementation guidelines"), "用于让代码改动保持聚焦，并明确假设和验收方式。"),
    (("tdd", "test-driven development", "red-green-refactor"), "用于按先写测试、再实现、后整理的循环开发功能。"),
    (("code-debugging", "debugging", "debug code", "troubleshoot code"), "用于定位代码或实验中的错误，并分步骤修复问题。"),
    (("write-swift", "swift 6", "swift concurrency"), "用于编写现代 Swift 代码并处理并发、安全和性能问题。"),
    (("animate-expo",), "用于在 Expo 应用中实现动画、手势与页面过渡。"),
    (("find-animation-opportunities",), "用于检查界面与代码，找出适合添加动效的场景。"),
    (("improve-animations",), "用于审视现有动画，并制定有优先级的改进方案。"),
    (("review-animations",), "用于检查动画实现的节奏、细节和无障碍表现。"),
    (("apple-design",), "用于把 Apple 风格的动效、排版与空间层次原则用于界面设计。"),
    (("emil-design-eng",), "用于打磨界面组件、视觉细节与交互反馈。"),
    (("emil-frontend",), "用于处理前端设计、组件选择、动效与界面体验问题。"),
    (("impeccable",), "用于审查并改进界面结构、可用性、无障碍和视觉细节。"),
    (("animation-vocabulary", "motion vocabulary", "animation terms"), "用于把动画效果的口语描述对应到准确的动效术语。"),
    (("animate", "animation", "motion design", "gesture animation", "micro-interaction"), "用于设计、实现或审查界面动画、动效与交互反馈。"),
    (("pick-ui-library", "choose a ui library", "component library"), "用于为界面组件和交互选择合适的前端库。"),
    (("ask-sonner", "sonner", "toast notifications"), "用于接入或排查 Sonner 消息提示组件。"),
    (("landing-page-generator", "landing page", "conversion page"), "用于设计或优化活动落地页及其转化流程。"),
    (("frontend-design", "frontend", "front-end", "user interface", "ux review", "design system"), "用于设计、实现或改进网站和应用界面体验。"),
    (("pdf-ocr", "ocr", "scanned pdf", "text recognition"), "用于识别扫描文档或图片中的文字，并提取可用内容。"),
    (("pdf", "pdf document"), "用于读取、处理或生成 PDF 文档。"),
    (("spreadsheet", "excel", "csv", "表格"), "用于创建、编辑或分析电子表格与表格数据。"),
    (("ppt-master", "powerpoint", "presentation slides", "pptx"), "用于根据资料生成、编辑和整理演示文稿。"),
    (("json-canvas", "canvas file", "mind map", "flowchart"), "用于创建和编辑画布图、流程图或思维导图。"),
    (("archify", "interactive diagram", "architecture diagram", "knowledge map"), "用于把架构、流程或知识关系整理成可交互图表。"),
    (("defuddle", "extract web content", "web page content", "clean markdown"), "用于提取网页正文并整理成便于阅读的 Markdown。"),
    (("personal-archive", "personal archive", "knowledge archive"), "用于归档长期有用的个人数字信息、学习资料和想法。"),
    (("humanize-chinese", "writing-anti-ai", "humanize writing", "ai writing patterns"), "用于改善中文或英文文本表达，减少生硬和模板化写作痕迹。"),
    (("writing", "copywriting", "content writing"), "用于起草、改写或润色文本内容。"),
    (("security", "privacy", "vulnerability", "secure coding"), "用于识别安全、权限或隐私风险，并提出防护建议。"),
    (("git", "repository", "version control"), "用于管理 Git 代码仓库和协作开发流程。"),
    (("prototype", "visual picker", "ui prototype"), "用于生成并比较多种界面方案，快速确定设计方向。"),
)


def summarize_skill_zh(name: str, description: str, category: str | None = None) -> str:
    name_text = normalized_name(name)
    description_text = normalized_name(description)
    for keywords, summary in SUMMARY_RULES_ZH:
        if any(_contains_category_keyword(name_text, keyword) for keyword in keywords):
            return summary
    for keywords, summary in SUMMARY_RULES_ZH:
        if any(_contains_category_keyword(description_text, keyword) for keyword in keywords):
            return summary
    if re.search(r"[\u4e00-\u9fff]", description or "") and len(description.strip()) <= 35:
        return description.strip()
    resolved_category = category or classify_skill(name, description)
    category_summaries = {
        "前端与动效": "用于设计、实现或改进前端界面与交互体验。",
        "开发与调试": "用于辅助软件开发、代码检查或问题调试。",
        "系统与运维": "用于检查、配置或维护本地系统与开发环境。",
        "文档与办公": "用于创建、整理或转换文档与办公资料。",
        "AI与写作规范": "用于改进 AI 技能、提示词或文本工作流程。",
        "安全与规范": "用于检查安全与隐私风险，并改进防护措施。",
        "其他工具": f"用于辅助完成“{name.strip() or '该技能'}”相关任务。",
    }
    return category_summaries.get(resolved_category, category_summaries["其他工具"])


def load_latest_scan() -> dict:
    try:
        payload = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        for skill in payload.get("skills", []):
            skill["auto_category"] = classify_skill(skill.get("name", ""), skill.get("description", ""))
            skill["auto_description_zh"] = summarize_skill_zh(
                skill.get("name", ""), skill.get("description", ""), skill["auto_category"]
            )
        return payload
    except (OSError, json.JSONDecodeError):
        with SCAN_LOCK:
            return scan()


def build_import_plan(scan_data: dict) -> dict:
    settings = read_settings()
    library_path = settings["library_path"]
    priorities = {root_id: index for index, root_id in enumerate(settings["source_priority"])}
    resolutions = {normalized_name(name): value for name, value in settings["conflict_resolutions"].items()}
    decisions = []
    conflict_groups: dict[str, list[dict]] = {}

    for skill in scan_data.get("skills", []):
        locations = sorted(
            skill.get("locations", []),
            key=lambda item: (priorities.get(item["root_id"], len(priorities)), item["is_symlink"], item["path"].casefold()),
        )
        if not locations:
            continue
        canonical = locations[0]
        relative = Path(canonical["relative_path"])
        directory_name = relative.stem if relative.suffix.casefold() == ".md" else relative.name
        target_path = library_path / directory_name
        physical_locations: dict[str, dict] = {}
        for location in locations:
            physical_locations.setdefault(os.path.normcase(location["resolved_path"]), location)
        canonical_physical = os.path.normcase(canonical["resolved_path"])
        cleanup_candidates = [
            {
                "path": location["resolved_path"],
                "represented_by": location["path"],
                "root_label": location["root_label"],
            }
            for physical_path, location in physical_locations.items()
            if physical_path != canonical_physical
        ]
        agents = sorted({agent for location in locations for agent in location.get("agents", [])})
        library_location = next((location for location in locations if location["root_id"] == "skill-shelf-library"), None)
        resolution = resolutions.get(normalized_name(skill["name"])) if skill.get("name_conflict") else None
        resolution_matches = bool(resolution) and (
            resolution.get("preferred_content_hash") == skill["content_hash"]
            or any(location["root_id"] == resolution.get("preferred_root_id") for location in locations)
        )
        if skill.get("name_conflict") and not resolution:
            action = "review_conflict"
            reason = "同名 Skill 存在不同内容版本，必须人工选择或改名"
        elif skill.get("name_conflict") and not resolution_matches:
            action = "skip_conflict_variant"
            reason = "已按冲突解决规则保留另一个版本"
        elif library_location:
            action = "keep_in_library"
            reason = "统一库中已有相同内容原件"
        else:
            action = "import_to_library"
            reason = "统一库中尚无受管原件"
        decision = {
            "skill_id": skill["id"],
            "name": skill["name"],
            "content_hash": skill["content_hash"],
            "action": action,
            "reason": reason,
            "canonical_source": canonical,
            "target_path": str(target_path),
            "agents_detected": agents,
            "all_locations": locations,
            "cleanup_candidates": cleanup_candidates,
            "cleanup_requires_confirmation": bool(cleanup_candidates),
            "conflict_resolution": resolution if skill.get("name_conflict") else None,
        }
        decisions.append(decision)
        if skill.get("name_conflict"):
            conflict_groups.setdefault(normalized_name(skill["name"]), []).append({
                "skill_id": skill["id"],
                "content_hash": skill["content_hash"],
                "canonical_source": canonical,
            })

    target_groups: dict[str, list[dict]] = {}
    for decision in decisions:
        if decision["action"] == "import_to_library":
            target_groups.setdefault(os.path.normcase(decision["target_path"]), []).append(decision)
    for grouped in target_groups.values():
        if len({item["content_hash"] for item in grouped}) > 1:
            for decision in grouped:
                decision["action"] = "review_target_collision"
                decision["reason"] = "多个不同 Skill 将写入同一个目标目录，必须先改名"

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "based_on_scan": scan_data.get("scanned_at"),
        "dry_run": True,
        "library_path": str(library_path),
        "library_exists": library_path.is_dir(),
        "source_priority": settings["source_priority"],
        "summary": {
            "total_variants": len(decisions),
            "ready_to_import": sum(item["action"] == "import_to_library" for item in decisions),
            "managed_in_library": sum(item["action"] == "keep_in_library" for item in decisions),
            "blocked_variants": sum(item["action"].startswith("review_") for item in decisions),
            "skipped_conflict_variants": sum(item["action"] == "skip_conflict_variant" for item in decisions),
            "conflict_groups": len(conflict_groups),
            "resolved_conflict_groups": sum(name in resolutions for name in conflict_groups),
            "duplicate_physical_copies": sum(len(item["cleanup_candidates"]) for item in decisions),
        },
        "conflicts": [
            {"name": name, "variants": variants}
            for name, variants in sorted(conflict_groups.items())
        ],
        "decisions": decisions,
        "safety": {
            "files_changed": False,
            "skills_executed": False,
            "cleanup_applied": False,
            "note": "本计划仅供审阅；重复副本不会在导入阶段自动删除。",
        },
    }
    write_json_atomic(IMPORT_PLAN_PATH, plan)
    return plan


def build_deploy_plan(scan_data: dict, persist: bool = True) -> dict:
    settings = read_settings()
    library_path = settings["library_path"]
    import_plan = build_import_plan(scan_data)
    library_items = [
        item for item in import_plan["decisions"]
        if item["action"] == "keep_in_library"
    ]
    variants_by_path: dict[str, list[dict]] = {}
    for variant in scan_data.get("skills", []):
        for location in variant.get("locations", []):
            variants_by_path.setdefault(os.path.normcase(location["path"]), []).append(variant)

    targets = []
    all_decisions = []
    backup_root = library_path.parent / "backups" / "deploy-<timestamp>"
    for target in settings["deploy_targets"]:
        target_root = target["path"]
        decisions = []
        name_pattern = target.get("name_pattern")
        for item in library_items:
            library_location = next(
                location for location in item["all_locations"]
                if location["root_id"] == "skill-shelf-library"
            )
            directory_name = Path(library_location["relative_path"]).name
            source = library_path / directory_name
            destination = target_root / directory_name
            matched = variants_by_path.get(os.path.normcase(str(destination)), [])

            if name_pattern and not re.fullmatch(name_pattern, directory_name):
                action = "skip_incompatible"
                reason = f"目录名不符合 {target['label']} 的 Skill 命名规则"
            elif not matched and not destination.exists() and not destination.is_symlink():
                action = "create_link"
                reason = "目标中不存在同名条目，可创建目录联接"
            elif any(variant["content_hash"] != item["content_hash"] for variant in matched):
                action = "block_conflict"
                reason = "目标中存在同名不同内容，禁止替换"
            else:
                try:
                    linked_to_library = destination.resolve() == source.resolve() and destination.absolute() != destination.resolve()
                except OSError:
                    linked_to_library = False
                if linked_to_library:
                    action = "already_linked"
                    reason = "目标已经指向统一库"
                elif matched:
                    action = "backup_and_link"
                    reason = "目标是相同内容的物理副本，部署时先备份再替换为目录联接"
                else:
                    action = "block_unreadable"
                    reason = "目标存在但扫描器无法确认内容，禁止替换"

            decision = {
                "target_id": target["id"],
                "target_label": target["label"],
                "name": item["name"],
                "directory": directory_name,
                "content_hash": item["content_hash"],
                "source_path": str(source),
                "destination_path": str(destination),
                "action": action,
                "reason": reason,
                "backup_path": str(backup_root / target["id"] / directory_name) if action == "backup_and_link" else None,
            }
            decisions.append(decision)
            all_decisions.append(decision)

        targets.append({
            "id": target["id"],
            "label": target["label"],
            "path": str(target_root),
            "exists": target_root.is_dir(),
            "link_type": target.get("link_type", "junction"),
            "preserve_names": target.get("preserve_names", []),
            "summary": {
                "create_links": sum(item["action"] == "create_link" for item in decisions),
                "backup_and_link": sum(item["action"] == "backup_and_link" for item in decisions),
                "already_linked": sum(item["action"] == "already_linked" for item in decisions),
                "blocked": sum(item["action"].startswith("block_") for item in decisions),
                "skipped_incompatible": sum(item["action"] == "skip_incompatible" for item in decisions),
            },
            "decisions": decisions,
        })

    plan = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "based_on_scan": scan_data.get("scanned_at"),
        "dry_run": True,
        "library_path": str(library_path),
        "backup_root_template": str(backup_root),
        "summary": {
            "managed_skills": len(library_items),
            "target_count": len(targets),
            "create_links": sum(item["action"] == "create_link" for item in all_decisions),
            "backup_and_link": sum(item["action"] == "backup_and_link" for item in all_decisions),
            "already_linked": sum(item["action"] == "already_linked" for item in all_decisions),
            "blocked": sum(item["action"].startswith("block_") for item in all_decisions),
            "skipped_incompatible": sum(item["action"] == "skip_incompatible" for item in all_decisions),
        },
        "targets": targets,
        "safety": {
            "files_changed": False,
            "existing_entries_require_backup": True,
            "different_content_overwritten": False,
            "unmanaged_entries_removed": False,
            "note": "本计划不修改 Agent 目录。执行器应逐项备份相同副本，并在每个链接创建后验证目标。",
        },
    }
    if persist:
        write_json_atomic(DEPLOY_PLAN_PATH, plan)
    return plan


def create_directory_junction(source: Path, destination: Path) -> None:
    environment = os.environ.copy()
    environment["SKILL_SHELF_LINK_SOURCE"] = str(source)
    environment["SKILL_SHELF_LINK_DESTINATION"] = str(destination)
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "New-Item -ItemType Junction "
        "-Path $env:SKILL_SHELF_LINK_DESTINATION "
        "-Target $env:SKILL_SHELF_LINK_SOURCE | Out-Null"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "创建目录联接失败"
        raise OSError(message)


def is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def junction_target(path: Path) -> Path | None:
    if not is_reparse_point(path):
        return None
    try:
        target = os.readlink(path)
    except OSError:
        return None
    if target.startswith("\\\\?\\"):
        target = target[4:]
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    return candidate.resolve(strict=False)


def read_managed_links() -> list[dict]:
    try:
        data = json.loads(MANAGED_LINKS_PATH.read_text(encoding="utf-8"))
        return data.get("links", []) if isinstance(data.get("links"), list) else []
    except (OSError, json.JSONDecodeError):
        return []


def write_managed_links(links: list[dict]) -> None:
    unique = {}
    for item in links:
        unique[os.path.normcase(item["destination_path"])] = item
    write_json_atomic(MANAGED_LINKS_PATH, {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "links": sorted(unique.values(), key=lambda item: item["destination_path"].casefold()),
    })


def sync_managed_links(plan: dict) -> None:
    links = []
    for target in plan["targets"]:
        for decision in target["decisions"]:
            if decision["action"] != "already_linked":
                continue
            links.append({
                "target_id": target["id"],
                "directory": decision["directory"],
                "source_path": decision["source_path"],
                "destination_path": decision["destination_path"],
            })
    write_managed_links(links)


def remove_orphaned_managed_links() -> list[dict]:
    kept = []
    removed = []
    for item in read_managed_links():
        source = Path(item["source_path"])
        destination = Path(item["destination_path"])
        if source.exists():
            kept.append(item)
            continue
        target = junction_target(destination)
        if target is None or target != source.resolve(strict=False):
            kept.append(item)
            continue
        destination.rmdir()
        removed.append({**item, "status": "orphan_link_removed"})
    write_managed_links(kept)
    return removed


def move_to_recycle_bin(path: Path) -> None:
    environment = os.environ.copy()
    environment["SKILL_SHELF_RECYCLE_PATH"] = str(path)
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        "$target = $env:SKILL_SHELF_RECYCLE_PATH; "
        "if ([System.IO.Directory]::Exists($target)) { "
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory("
        "$target, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
        "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin) "
        "} elseif ([System.IO.File]::Exists($target)) { "
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        "$target, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
        "[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin) "
        "} else { throw '待回收路径不存在' }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "移入回收站失败"
        raise OSError(message)


def apply_deploy_plan() -> dict:
    fresh_scan = scan()
    plan = build_deploy_plan(fresh_scan)
    library = Path(plan["library_path"]).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = library.parent / "backups" / f"deploy-{timestamp}-{uuid.uuid4().hex[:8]}"
    allowed_deploy_roots = [
        item["path"].resolve()
        for item in read_config()
        if item["path"].exists()
    ]
    results = []

    for target in plan["targets"]:
        target_root = Path(target["path"])
        if target_root.exists() and (not target_root.is_dir() or target_root.is_symlink()):
            for decision in target["decisions"]:
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "failed",
                    "reason": "Agent 的 Skills 根路径必须是普通目录",
                })
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        target_root_resolved = target_root.resolve()

        for decision in target["decisions"]:
            action = decision["action"]
            if action not in {"create_link", "backup_and_link"}:
                status = "already_linked" if action == "already_linked" else "skipped"
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": status,
                    "reason": decision["reason"],
                })
                continue

            source = Path(decision["source_path"])
            destination = Path(decision["destination_path"])
            if source.parent.resolve() != library or not (source / "SKILL.md").is_file():
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "failed",
                    "reason": "统一库来源不合法或缺少 SKILL.md",
                })
                continue
            if destination.parent.resolve() != target_root_resolved:
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "failed",
                    "reason": "目标不在 Agent 的 Skills 根目录下",
                })
                continue

            backup = backup_root / target["id"] / decision["directory"]
            moved_to_backup = False
            link_created = False
            try:
                if action == "backup_and_link":
                    existing = fingerprint(destination, allowed_deploy_roots, {}, {})
                    if not existing or existing[0] != decision["content_hash"]:
                        raise ValueError("目标内容在生成计划后发生变化，已停止替换")
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if backup.exists() or backup.is_symlink():
                        raise FileExistsError("备份目标已存在")
                    os.replace(destination, backup)
                    moved_to_backup = True
                elif destination.exists() or destination.is_symlink():
                    raise FileExistsError("目标在生成计划后已被创建")

                create_directory_junction(source, destination)
                link_created = True
                if destination.resolve() != source.resolve() or not (destination / "SKILL.md").is_file():
                    raise OSError("目录联接验证失败")
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "linked",
                    "source_path": str(source),
                    "destination_path": str(destination),
                    "backup_path": str(backup) if moved_to_backup else None,
                })
            except Exception as exc:
                if link_created and (destination.is_symlink() or (destination.exists() and destination.absolute() != destination.resolve())):
                    destination.rmdir()
                if moved_to_backup and backup.exists() and not destination.exists():
                    os.replace(backup, destination)
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "failed",
                    "reason": str(exc),
                })

    result = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "library_path": str(library),
        "backup_root": str(backup_root) if backup_root.exists() else None,
        "summary": {
            "linked": sum(item["status"] == "linked" for item in results),
            "already_linked": sum(item["status"] == "already_linked" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        },
        "results": results,
        "safety": {
            "different_content_overwritten": False,
            "unmanaged_entries_removed": False,
            "same_content_copies_backed_up": True,
            "per_item_rollback_on_failure": True,
        },
    }
    write_json_atomic(DEPLOY_RESULT_PATH, result)
    return result


def build_deploy_status(scan_data: dict) -> dict:
    plan = build_deploy_plan(scan_data, persist=False)
    library = Path(plan["library_path"])
    backup_parent = library.parent / "backups"
    backup_roots = []
    if backup_parent.is_dir():
        for path in sorted(backup_parent.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_dir() and path.name.startswith("deploy-"):
                backup_roots.append({
                    "path": str(path),
                    "skill_backup_count": sum(1 for _ in path.rglob("SKILL.md")),
                })
    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "library_path": str(library),
        "summary": plan["summary"],
        "targets": [
            {
                "id": target["id"],
                "label": target["label"],
                "path": target["path"],
                "exists": target["exists"],
                "summary": target["summary"],
            }
            for target in plan["targets"]
        ],
        "backup_roots": backup_roots,
        "exceptions": [
            decision
            for target in plan["targets"]
            for decision in target["decisions"]
            if decision["action"].startswith("block_") or decision["action"] == "skip_incompatible"
        ],
    }
    return status


def reconcile_missing_links() -> dict:
    orphan_results = remove_orphaned_managed_links()
    scan_data = scan()
    plan = build_deploy_plan(scan_data)
    results = orphan_results
    for target in plan["targets"]:
        target_root = Path(target["path"])
        if target_root.exists() and (not target_root.is_dir() or target_root.is_symlink()):
            results.append({
                "target_id": target["id"],
                "status": "failed",
                "reason": "Agent 的 Skills 根路径必须是普通目录",
            })
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        for decision in target["decisions"]:
            if decision["action"] != "create_link":
                continue
            source = Path(decision["source_path"])
            destination = Path(decision["destination_path"])
            if destination.exists() or destination.is_symlink():
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "skipped",
                    "reason": "目标已经存在，未覆盖",
                })
                continue
            link_created = False
            try:
                create_directory_junction(source, destination)
                link_created = True
                if destination.resolve() != source.resolve() or not (destination / "SKILL.md").is_file():
                    raise OSError("目录联接验证失败")
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "linked",
                    "source_path": str(source),
                    "destination_path": str(destination),
                })
            except Exception as exc:
                if link_created and (destination.is_symlink() or (destination.exists() and destination.absolute() != destination.resolve())):
                    destination.rmdir()
                results.append({
                    "target_id": target["id"],
                    "directory": decision["directory"],
                    "status": "failed",
                    "reason": str(exc),
                })

    final_scan = scan() if any(item["status"] == "linked" for item in results) else scan_data
    status = build_deploy_status(final_scan)
    result = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "linked": sum(item["status"] == "linked" for item in results),
            "orphan_links_removed": sum(item["status"] == "orphan_link_removed" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        },
        "results": results,
        "deploy_status_summary": status["summary"],
        "safety": {
            "existing_entries_overwritten": False,
            "existing_entries_removed": False,
            "missing_links_only": True,
        },
    }
    write_json_atomic(RECONCILE_RESULT_PATH, result)
    return result


def refresh_skill(directory: str) -> dict:
    safe_name = Path(directory).name
    if safe_name != directory or safe_name.startswith("."):
        raise ValueError("Skill 目录名不合法")
    library = read_settings()["library_path"].resolve()
    source = library / safe_name
    if source.parent.resolve() != library or not (source / "SKILL.md").is_file():
        raise FileNotFoundError("统一库中不存在该 Skill 或缺少 SKILL.md")
    result = fingerprint(source, [library], {}, {})
    if not result:
        raise ValueError("无法读取 Skill 内容")
    reconcile = reconcile_missing_links()
    return {
        "directory": safe_name,
        "content_hash": result[0],
        "modified_at": datetime.fromtimestamp(result[1], timezone.utc).isoformat() if result[1] else None,
        "reconcile_summary": reconcile["summary"],
        "note": "各 Agent 使用目录联接，内容修改已直接生效。",
    }


def delete_skill_to_recycle_bin(directory: str, expected_hash: str | None = None) -> dict:
    safe_name = Path(directory).name
    if safe_name != directory or safe_name.startswith("."):
        raise ValueError("Skill 目录名不合法")
    library = read_settings()["library_path"].resolve()
    source = library / safe_name
    if source.parent.resolve() != library or not source.is_dir() or source.is_symlink():
        raise FileNotFoundError("统一库中不存在可删除的普通 Skill 目录")
    if expected_hash is not None:
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("待删除版本的内容指纹无效")
        current = fingerprint(source, [library], {}, {})
        if not current or current[0] != expected_hash:
            raise ValueError("统一库中的内容已经变化，请重新扫描并确认要删除的版本")

    removed_links = []
    retained_links = []
    try:
        for item in read_managed_links():
            destination = Path(item["destination_path"])
            if Path(item["source_path"]).resolve(strict=False) != source.resolve(strict=False):
                retained_links.append(item)
                continue
            target = junction_target(destination)
            if target == source.resolve(strict=False):
                destination.rmdir()
                removed_links.append(item)
            else:
                retained_links.append(item)
        move_to_recycle_bin(source)
    except Exception:
        for item in removed_links:
            destination = Path(item["destination_path"])
            if not destination.exists() and not is_reparse_point(destination):
                create_directory_junction(source, destination)
        raise
    write_managed_links(retained_links)
    sources = load_sources()
    sources.pop(safe_name, None)
    save_sources(sources)
    result = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "directory": safe_name,
        "source_moved_to_recycle_bin": True,
        "removed_managed_links": len(removed_links),
        "retained_nonmanaged_entries": True,
    }
    write_json_atomic(DELETE_RESULT_PATH, result)
    scan_data = scan()
    result["deploy_status"] = build_deploy_status(scan_data)["summary"]
    write_json_atomic(DELETE_RESULT_PATH, result)
    return result


def library_signature(library: Path) -> tuple:
    if not library.is_dir():
        return ()
    rows = []
    try:
        children = sorted(library.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return ()
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        manifest = child / "SKILL.md"
        if not manifest.is_file():
            continue
        digest = hashlib.sha256()
        try:
            for file_path, relative, file_stat in collect_file_rows(child):
                digest.update(relative.encode("utf-8", errors="replace"))
                digest.update(b"\0")
                digest.update(str(file_stat.st_size).encode("ascii"))
                digest.update(b"\0")
                digest.update(str(file_stat.st_mtime_ns).encode("ascii"))
                digest.update(b"\0")
        except OSError:
            continue
        rows.append((child.name.casefold(), digest.hexdigest()))
    return tuple(rows)


def watch_library() -> None:
    settings = read_settings()
    if not settings["auto_link_new_skills"]:
        return
    library = settings["library_path"]
    interval = settings["auto_link_poll_seconds"]
    previous = None
    while True:
        current = library_signature(library)
        if current != previous:
            try:
                with SCAN_LOCK:
                    reconcile_missing_links()
            except Exception as exc:
                print(f"自动补建 Skill 联接失败：{exc}")
            previous = library_signature(library)
        threading.Event().wait(interval)


def find_nested_links(source: Path) -> list[str]:
    if source.is_file():
        return [str(source)] if is_reparse_point(source) else []
    links = []
    for current, directories, filenames in os.walk(source, followlinks=False):
        for name in directories + filenames:
            candidate = Path(current) / name
            if is_reparse_point(candidate):
                links.append(str(candidate))
    return links


def copy_skill(source: Path, destination: Path) -> None:
    nested_links = find_nested_links(source)
    if nested_links:
        raise ValueError(f"Skill 包含嵌套链接，已停止复制：{nested_links[0]}")
    destination.mkdir(parents=True, exist_ok=False)
    if source.is_file():
        shutil.copy2(source, destination / "SKILL.md")
        return
    for file_path, relative, _ in collect_file_rows(source):
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)


def conflict_variants_for_name(scan_data: dict, name: str) -> list[dict]:
    key = normalized_name(name)
    variants = [
        item for item in scan_data.get("skills", [])
        if item.get("name_conflict") and normalized_name(item.get("name", "")) == key
    ]
    if len(variants) < 2:
        raise ValueError("该 Skill 当前没有待处理的同名冲突，请重新扫描")
    return sorted(variants, key=lambda item: item["content_hash"])


def save_conflict_preference(name: str, content_hash: str) -> tuple[Path, dict, dict]:
    config_path = CONFIG_PATH if CONFIG_PATH.is_file() else RESOURCE_DIR / "roots.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    previous = json.loads(json.dumps(data, ensure_ascii=False))
    resolutions = dict(data.get("conflict_resolutions", {}))
    key = normalized_name(name)
    for existing_name in list(resolutions):
        if normalized_name(existing_name) == key:
            resolutions.pop(existing_name, None)
    resolutions[name] = {
        "preferred_content_hash": content_hash,
        "reason": "已在冲突处理器中由用户统一选择。",
    }
    data["conflict_resolutions"] = resolutions
    return config_path, previous, data


def conflict_has_single_file(variants: list[dict]) -> bool:
    return any(Path(location["resolved_path"]).is_file()
               for variant in variants for location in variant.get("locations", []))


def build_conflict_overview(scan_data: dict) -> dict:
    settings = read_settings()
    priorities = {root_id: index for index, root_id in enumerate(settings["source_priority"])}
    resolutions = {
        normalized_name(name): value
        for name, value in settings["conflict_resolutions"].items()
    }
    groups: dict[str, list[dict]] = {}
    for item in scan_data.get("skills", []):
        if item.get("name_conflict"):
            groups.setdefault(normalized_name(item["name"]), []).append(item)

    result = []
    for key, variants in sorted(groups.items()):
        resolution = resolutions.get(key, {})
        configured_hash = resolution.get("preferred_content_hash")
        configured_root = resolution.get("preferred_root_id")
        recommended = next(
            (item for item in variants if item["content_hash"] == configured_hash),
            None,
        )
        if recommended is None and configured_root:
            recommended = next(
                (item for item in variants if any(
                    location["root_id"] == configured_root for location in item["locations"]
                )),
                None,
            )
        if recommended is None:
            recommended = next(
                (item for item in variants if any(
                    location["root_id"] == "skill-shelf-library" for location in item["locations"]
                )),
                None,
            )
        if recommended is None:
            recommended = max(
                variants,
                key=lambda item: (
                    item.get("modified_at") or "",
                    -min((priorities.get(location["root_id"], len(priorities))
                          for location in item["locations"]), default=len(priorities)),
                ),
            )

        has_library_version = any(
            location["root_id"] == "skill-shelf-library"
            for item in variants for location in item["locations"]
        )
        recommendation_reason = (
            "统一库已有此版本，优先保留可减少 Agent 切换和重新部署。"
            if has_library_version
            else "统一库尚无此名称，推荐以本地更新时间较新的版本作为起点；时间仅供参考。"
        )
        all_physical_paths = {
            os.path.normcase(location["resolved_path"])
            for item in variants for location in item["locations"]
        }
        single_file_conflict = conflict_has_single_file(variants)
        result.append({
            "key": key,
            "name": variants[0]["name"],
            "expected_hashes": [item["content_hash"] for item in variants],
            "physical_copy_count": len(all_physical_paths),
            "recommendation": {
                "mode": "keep",
                "content_hash": recommended["content_hash"],
                "reason": recommendation_reason,
            },
            "resolution_supported": not single_file_conflict,
            "resolution_block_reason": (
                "此冲突包含独立 Markdown 文件；全局联接统一目前需要先将它转换为带 SKILL.md 的目录。"
                if single_file_conflict else None
            ),
            "variants": [
                {
                    "name": item["name"],
                    "content_hash": item["content_hash"],
                    "description": item.get("description", ""),
                    "auto_description_zh": item.get("auto_description_zh", ""),
                    "modified_at": item.get("modified_at"),
                    "is_library_version": any(
                        location["root_id"] == "skill-shelf-library"
                        for location in item["locations"]
                    ),
                    "physical_copy_count": item.get("physical_copy_count", 0),
                    "locations": item.get("locations", []),
                }
                for item in variants
            ],
        })
    return {"groups": result}


def conflict_source_for_variant(variant: dict, settings: dict, allowed_roots: list[Path]) -> tuple[Path, dict]:
    priorities = {root_id: index for index, root_id in enumerate(settings["source_priority"])}
    locations = sorted(
        variant.get("locations", []),
        key=lambda item: (
            item["root_id"] != "skill-shelf-library",
            item.get("is_symlink", False),
            priorities.get(item["root_id"], len(priorities)),
            item["path"].casefold(),
        ),
    )
    for location in locations:
        source = Path(location["resolved_path"])
        current = fingerprint(source, allowed_roots, {}, {})
        if current and current[0] == variant["content_hash"]:
            return source, location
    raise ValueError(f"无法重新读取 Skill 版本：{variant.get('name', '')}")


def conflict_file_digest(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.casefold() in TEXT_SUFFIXES or path.name.casefold() == "skill.md":
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def collect_conflict_files(
    variants: list[dict], settings: dict, allowed_roots: list[Path]
) -> tuple[dict[str, list[dict]], dict[str, tuple[Path, dict]]]:
    files: dict[str, list[dict]] = {}
    sources = {}
    for variant in variants:
        source, location = conflict_source_for_variant(variant, settings, allowed_roots)
        sources[variant["content_hash"]] = (source, location)
        for file_path, relative, _ in collect_file_rows(source):
            files.setdefault(relative, []).append({
                "variant_hash": variant["content_hash"],
                "file_hash": conflict_file_digest(file_path),
                "source_path": file_path,
                "source_label": location["root_label"],
            })
    return files, sources


def build_conflict_merge_preview(scan_data: dict, name: str, expected_hashes: list[str] | None = None) -> dict:
    variants = conflict_variants_for_name(scan_data, name)
    hashes = sorted(item["content_hash"] for item in variants)
    if expected_hashes is not None and sorted(expected_hashes) != hashes:
        raise ValueError("冲突版本在预览后发生变化，请重新打开冲突方案")

    settings = read_settings()
    allowed_roots = [item["path"].resolve() for item in settings["roots"] if item["path"].exists()]
    files, sources = collect_conflict_files(variants, settings, allowed_roots)
    if any(source.is_file() for source, _ in sources.values()):
        return {
            "name": variants[0]["name"],
            "expected_hashes": hashes,
            "merge_supported": False,
            "reason": "此冲突包含独立 Markdown 文件；全局联接统一目前需要先将它转换为带 SKILL.md 的目录。",
            "file_count": len(files),
            "collisions": [],
        }
    linked_sources = [
        str(link)
        for source, _ in sources.values()
        for link in find_nested_links(source)
    ]
    if linked_sources:
        return {
            "name": variants[0]["name"],
            "expected_hashes": hashes,
            "merge_supported": False,
            "reason": f"版本包含内部文件链接，合并会改变其指向；请先单独检查：{linked_sources[0]}",
            "file_count": len(files),
            "collisions": [],
        }

    overview = build_conflict_overview(scan_data)
    group = next(item for item in overview["groups"] if item["key"] == normalized_name(name))
    recommended_hash = group["recommendation"]["content_hash"]
    collisions = []
    for relative, candidates in sorted(files.items(), key=lambda item: item[0].casefold()):
        if len({item["file_hash"] for item in candidates}) < 2:
            continue
        options = []
        for candidate in candidates:
            if any(item["content_hash"] == candidate["variant_hash"] for item in options):
                continue
            variant = next(item for item in variants if item["content_hash"] == candidate["variant_hash"])
            options.append({
                "content_hash": candidate["variant_hash"],
                "hash_prefix": candidate["variant_hash"][:12],
                "source_label": candidate["source_label"],
                "modified_at": variant.get("modified_at"),
            })
        option_hashes = {item["content_hash"] for item in options}
        file_recommendation = recommended_hash if recommended_hash in option_hashes else max(
            options, key=lambda item: item.get("modified_at") or ""
        )["content_hash"]
        collisions.append({
            "path": relative,
            "variants": options,
            "recommended_hash": file_recommendation,
        })

    return {
        "name": variants[0]["name"],
        "expected_hashes": hashes,
        "merge_supported": True,
        "file_count": len(files),
        "collision_count": len(collisions),
        "collisions": collisions,
    }


def conflict_directory_name(location: dict) -> str:
    source = Path(location["resolved_path"])
    if source.is_file():
        return source.stem
    relative = Path(location["relative_path"])
    if str(relative) in {"", "."}:
        return Path(location["path"]).name
    return relative.name


def validate_skill_directory_name(name: str) -> None:
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    if (
        not name or name in {".", ".."} or name.startswith(".")
        or re.search(r'[<>:"/\\|?*]', name) or name.endswith((".", " "))
        or name.split(".", 1)[0].upper() in reserved
    ):
        raise ValueError("统一后的 Skill 目录名不适用于 Windows，请先调整现有目录名")


def copy_merged_skill(
    variants: list[dict],
    settings: dict,
    allowed_roots: list[Path],
    staging: Path,
    file_choices: dict,
    recommended_hash: str,
) -> None:
    files, _ = collect_conflict_files(variants, settings, allowed_roots)
    required_choices = {
        relative for relative, candidates in files.items()
        if len({item["file_hash"] for item in candidates}) > 1
    }
    if set(file_choices) != required_choices:
        raise ValueError("融合方案中的文件选择不完整，请重新预览")

    staging.mkdir(parents=True, exist_ok=False)
    for relative, candidates in files.items():
        posix_path = PurePosixPath(relative)
        if posix_path.is_absolute() or ".." in posix_path.parts or not posix_path.parts:
            raise ValueError("Skill 中包含无效的相对文件路径")
        distinct_hashes = {item["file_hash"] for item in candidates}
        if len(distinct_hashes) > 1:
            selected_hash = file_choices[relative]
            source = next(
                (item for item in candidates if item["variant_hash"] == selected_hash),
                None,
            )
            if source is None:
                raise ValueError(f"文件选择引用了不存在的版本：{relative}")
        else:
            source = next(
                (item for item in candidates if item["variant_hash"] == recommended_hash),
                candidates[0],
            )
        target = staging.joinpath(*posix_path.parts)
        if not inside(target.resolve(strict=False), staging.resolve()):
            raise ValueError("Skill 中包含越界文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source["source_path"], target)


def apply_conflict_resolution(request: dict) -> dict:
    if request.get("confirm") != "RESOLVE_SKILL_CONFLICT":
        raise ValueError("缺少冲突处理确认")
    name = request.get("name")
    mode = request.get("mode")
    expected_hashes = request.get("expected_hashes")
    if not isinstance(name, str) or not name.strip() or len(name) > 512:
        raise ValueError("Skill 名称无效")
    if mode not in {"keep", "merge"}:
        raise ValueError("冲突处理方式无效")
    if not isinstance(expected_hashes, list) or not all(
        isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        for item in expected_hashes
    ):
        raise ValueError("冲突版本列表无效，请重新扫描")

    scan_data = scan()
    variants = conflict_variants_for_name(scan_data, name)
    hashes = sorted(item["content_hash"] for item in variants)
    if sorted(expected_hashes) != hashes:
        raise ValueError("冲突版本在确认前发生变化，请重新扫描后选择")
    if conflict_has_single_file(variants):
        raise ValueError("此冲突包含独立 Markdown 文件；请先将它转换为带 SKILL.md 的目录，再进行全局统一")

    overview = build_conflict_overview(scan_data)
    group = next(item for item in overview["groups"] if item["key"] == normalized_name(name))
    if mode == "keep":
        selected_hash = request.get("selected_hash")
        selected_variant = next(
            (item for item in variants if item["content_hash"] == selected_hash),
            None,
        )
        if selected_variant is None:
            raise ValueError("所选版本已不存在，请重新扫描")
        recommended_hash = selected_hash
        file_choices = {}
    else:
        selected_variant = None
        recommended_hash = group["recommendation"]["content_hash"]
        file_choices = request.get("file_choices", {})
        if not isinstance(file_choices, dict) or not all(
            isinstance(path, str) and isinstance(digest, str)
            for path, digest in file_choices.items()
        ):
            raise ValueError("融合文件选择无效")

    settings = read_settings()
    roots = {item["id"]: item for item in settings["roots"]}
    library = settings["library_path"]
    if library.exists() and (not library.is_dir() or is_reparse_point(library)):
        raise ValueError("统一库路径必须是普通目录")
    allowed_roots = [item["path"].resolve() for item in settings["roots"] if item["path"].exists()]

    library_locations = [
        location
        for variant in variants
        for location in variant["locations"]
        if location["root_id"] == "skill-shelf-library"
    ]
    if library_locations:
        directory_name = conflict_directory_name(library_locations[0])
    else:
        directory_variant = selected_variant or next(
            item for item in variants if item["content_hash"] == recommended_hash
        )
        _, directory_location = conflict_source_for_variant(directory_variant, settings, allowed_roots)
        directory_name = conflict_directory_name(directory_location)
    validate_skill_directory_name(directory_name)
    library.mkdir(parents=True, exist_ok=True)
    library_resolved = library.resolve()
    target = library / directory_name
    target_resolved = target.resolve(strict=False)
    target_location_known = any(
        Path(location["path"]).resolve(strict=False) == target_resolved
        for location in library_locations
    )
    if (target.exists() or is_reparse_point(target)) and not target_location_known:
        raise ValueError("统一库中已有未归入本次冲突的同名目录，未覆盖")
    if target.exists() and (not target.is_dir() or is_reparse_point(target)):
        raise ValueError("统一库中的目标必须是普通 Skill 目录")

    current_library = fingerprint(target, [library_resolved], {}, {}) if target.exists() else None
    if target.exists() and (not current_library or current_library[0] not in hashes):
        raise ValueError("统一库目标已变化，请重新扫描后处理冲突")

    staging = library / f".conflict-stage-{uuid.uuid4().hex}"
    output_hash = None
    if mode == "keep" and current_library and current_library[0] == selected_variant["content_hash"]:
        output_hash = current_library[0]
    else:
        try:
            if mode == "keep":
                source, _ = conflict_source_for_variant(selected_variant, settings, allowed_roots)
                copy_skill(source, staging)
            else:
                preview = build_conflict_merge_preview(scan_data, name, hashes)
                if not preview["merge_supported"]:
                    raise ValueError(preview["reason"])
                copy_merged_skill(
                    variants, settings, allowed_roots, staging, file_choices, recommended_hash
                )
            manifest_name, _ = read_manifest(staging, directory_name)
            if normalized_name(manifest_name) != normalized_name(name):
                raise ValueError("统一后的 SKILL.md 名称与冲突组不一致")
            staged = fingerprint(staging, [library_resolved], {}, {})
            if not staged:
                raise ValueError("无法读取统一后的 Skill 内容")
            if mode == "keep" and staged[0] != selected_variant["content_hash"]:
                raise ValueError("所选版本复制后校验不一致，请重新扫描")
            output_hash = staged[0]
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    stage_needed = staging.exists()
    if stage_needed and current_library and current_library[0] == output_hash:
        shutil.rmtree(staging)
        stage_needed = False

    physical_sources: dict[str, dict] = {}
    remove_links: dict[str, dict] = {}
    affected_roots: dict[str, dict] = {}
    for variant in variants:
        for location in variant["locations"]:
            root = roots.get(location["root_id"])
            if root is None:
                raise ValueError("冲突位置不在当前已登记根目录中")
            path = Path(location["path"])
            root_resolved = root["path"].resolve(strict=False)
            if path.parent.resolve(strict=False) != root_resolved:
                raise ValueError(f"冲突位置不在登记根目录的直接子目录中：{path}")
            current = fingerprint(path, allowed_roots, {}, {})
            if not current or current[0] != variant["content_hash"]:
                raise ValueError(f"冲突位置内容已变化：{path}")
            resolved_path = Path(location["resolved_path"]).resolve(strict=False)
            if location["root_id"] != "skill-shelf-library" and root_resolved != library_resolved:
                affected_roots[location["root_id"]] = root

            if os.path.normcase(str(resolved_path)) == os.path.normcase(str(target_resolved)):
                continue
            path_key = os.path.normcase(str(path.absolute()))
            if is_reparse_point(path):
                link_target = junction_target(path)
                if link_target and link_target.is_file():
                    raise ValueError(f"冲突位置包含指向文件的链接，暂不能自动统一：{path}")
                remove_links[path_key] = {
                    "path": path,
                    "target": link_target,
                    "directory": path.is_dir(),
                }
                continue

            physical_key = os.path.normcase(str(resolved_path))
            physical_sources.setdefault(physical_key, {
                "path": path,
                "resolved_path": resolved_path,
                "root_id": location["root_id"],
                "expected_hash": variant["content_hash"],
            })

    desired_links: dict[str, dict] = {}
    for root_id, root in affected_roots.items():
        destination = root["path"] / directory_name
        if root["path"].resolve(strict=False) == library_resolved:
            continue
        desired_links[os.path.normcase(str(destination.absolute()))] = {
            "target_id": root_id,
            "destination": destination,
        }
    for target_info in settings["deploy_targets"]:
        pattern = target_info.get("name_pattern")
        if pattern and not re.fullmatch(pattern, directory_name):
            raise ValueError(f"统一目录名不符合 {target_info['label']} 的命名要求：{directory_name}")
        destination = target_info["path"] / directory_name
        if target_info["path"].resolve(strict=False) == library_resolved:
            continue
        desired_links[os.path.normcase(str(destination.absolute()))] = {
            "target_id": target_info["id"],
            "destination": destination,
        }

    physical_keys = set(physical_sources)
    physical_absolute_paths = {
        os.path.normcase(str(item["path"].absolute()))
        for item in physical_sources.values()
    }
    removable_link_paths = set(remove_links)
    for item in desired_links.values():
        destination = item["destination"]
        if not destination.exists() and not is_reparse_point(destination):
            continue
        resolved_destination = destination.resolve(strict=False)
        if os.path.normcase(str(resolved_destination)) == os.path.normcase(str(target_resolved)):
            continue
        if (
            os.path.normcase(str(destination.absolute())) in physical_absolute_paths
            or os.path.normcase(str(resolved_destination)) in physical_keys
            or os.path.normcase(str(destination.absolute())) in removable_link_paths
        ):
            continue
        raise ValueError(f"目标位置已有不属于本次冲突的内容，未覆盖：{destination}")

    token = uuid.uuid4().hex
    quarantined = []
    removed_links = []
    created_links = []
    old_library_backup = None
    new_library_installed = False
    previous_managed_links = read_managed_links()
    config_path, previous_config, updated_config = save_conflict_preference(name, output_hash)
    config_updated = False
    try:
        previous_sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8")) if SOURCES_PATH.is_file() else {}
    except json.JSONDecodeError as exc:
        raise ValueError("Skill 来源记录 JSON 已损坏，请先修复后再处理冲突") from exc
    if not isinstance(previous_sources, dict):
        raise ValueError("Skill 来源记录格式无效，请先修复后再处理冲突")
    updated_sources = dict(previous_sources)
    if not current_library or current_library[0] != output_hash:
        updated_sources.pop(directory_name, None)
    sources_updated = False
    try:
        if stage_needed:
            if target.exists():
                old_library_backup = library.parent / f".{directory_name}.conflict-old-{token}"
                os.replace(target, old_library_backup)
            os.replace(staging, target)
            new_library_installed = True
            installed = fingerprint(target, [library_resolved], {}, {})
            if not installed or installed[0] != output_hash:
                raise OSError("统一库安装后的内容校验失败")

        for source_info in physical_sources.values():
            source = source_info["path"]
            if not source.exists() and not is_reparse_point(source):
                raise FileNotFoundError(f"冲突位置已不存在：{source}")
            latest = fingerprint(source, allowed_roots, {}, {})
            if not latest or latest[0] != source_info["expected_hash"]:
                raise ValueError(f"冲突位置在统一过程中发生变化：{source}")
            quarantine = source.with_name(f".{source.name}.conflict-{token}")
            if quarantine.exists() or is_reparse_point(quarantine):
                raise FileExistsError(f"临时整理路径已存在：{quarantine}")
            os.replace(source, quarantine)
            quarantined.append({"source": source, "quarantine": quarantine})

        for link_info in remove_links.values():
            link_path = link_info["path"]
            if not is_reparse_point(link_path):
                continue
            if link_info["directory"]:
                link_path.rmdir()
            else:
                link_path.unlink()
            removed_links.append(link_info)

        linked_results = []
        for link_info in desired_links.values():
            destination = link_info["destination"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or is_reparse_point(destination):
                if destination.resolve(strict=False) != target.resolve():
                    raise FileExistsError(f"统一位置在确认后被占用：{destination}")
            else:
                create_directory_junction(target, destination)
                created_links.append(destination)
            if destination.resolve() != target.resolve() or not (destination / "SKILL.md").is_file():
                raise OSError(f"统一目录联接验证失败：{destination}")
            linked_results.append({
                "target_id": link_info["target_id"],
                "source_path": str(target),
                "destination_path": str(destination),
            })

        removed_path_keys = physical_absolute_paths | removable_link_paths
        managed_links = [
            item for item in previous_managed_links
            if os.path.normcase(item["destination_path"]) not in removed_path_keys
        ]
        managed_links.extend({
            "target_id": item["target_id"],
            "directory": directory_name,
            "source_path": str(target),
            "destination_path": item["destination_path"],
        } for item in linked_results)
        write_managed_links(managed_links)
        config_updated = True
        write_json_atomic(config_path, updated_config)
        if updated_sources != previous_sources:
            sources_updated = True
            save_sources(updated_sources)
    except Exception as exc:
        rollback_errors = []
        for destination in reversed(created_links):
            try:
                if junction_target(destination) == target.resolve(strict=False):
                    destination.rmdir()
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if new_library_installed and target.exists():
            try:
                shutil.rmtree(target)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if old_library_backup and old_library_backup.exists() and not target.exists():
            try:
                os.replace(old_library_backup, target)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for item in reversed(quarantined):
            try:
                if item["quarantine"].exists() and not item["source"].exists():
                    os.replace(item["quarantine"], item["source"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for item in removed_links:
            try:
                link_target = item["target"]
                if link_target and link_target.exists() and not item["path"].exists():
                    create_directory_junction(link_target, item["path"])
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            write_managed_links(previous_managed_links)
        except OSError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if config_updated:
            try:
                write_json_atomic(config_path, previous_config)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if sources_updated:
            try:
                save_sources(previous_sources)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if rollback_errors:
            raise OSError(f"冲突统一失败且回滚未完全完成：{'; '.join(rollback_errors)}") from exc
        raise

    recycled = []
    failed_recycles = []
    for item in quarantined:
        try:
            move_to_recycle_bin(item["quarantine"])
            recycled.append(str(item["source"]))
        except Exception as exc:
            failed_recycles.append({"path": str(item["quarantine"]), "reason": str(exc)})
    if old_library_backup and old_library_backup.exists():
        try:
            move_to_recycle_bin(old_library_backup)
            recycled.append(str(target))
        except Exception as exc:
            failed_recycles.append({"path": str(old_library_backup), "reason": str(exc)})

    final_scan = scan()
    remaining_conflict = any(
        item.get("name_conflict") and normalized_name(item.get("name", "")) == normalized_name(name)
        for item in final_scan.get("skills", [])
    )
    result = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "mode": mode,
        "library_path": str(target),
        "content_hash": output_hash,
        "summary": {
            "recycled_physical_copies": len(recycled),
            "linked_locations": len(linked_results),
            "failed_recycles": len(failed_recycles),
            "remaining_conflict": remaining_conflict,
        },
        "recycled_paths": recycled,
        "failed_recycles": failed_recycles,
        "scan": final_scan,
        "safety": {
            "extra_copies_sent_to_recycle_bin": True,
            "permanent_delete": False,
            "links_verified": True,
        },
    }
    write_json_atomic(CONFLICT_RESULT_PATH, result)
    return result


def apply_import_plan() -> dict:
    fresh_scan = scan()
    plan = build_import_plan(fresh_scan)
    library = Path(plan["library_path"])
    if library.exists() and (not library.is_dir() or library.is_symlink()):
        raise ValueError("统一库路径必须是普通目录，不能是文件或链接")
    library.mkdir(parents=True, exist_ok=True)
    library_resolved = library.resolve()
    results = []

    for decision in plan["decisions"]:
        if decision["action"] != "import_to_library":
            if decision["action"].startswith("review_"):
                status = "blocked"
            elif decision["action"] == "skip_conflict_variant":
                status = "skipped"
            else:
                status = "already_present"
            results.append({
                "skill_id": decision["skill_id"],
                "name": decision["name"],
                "status": status,
                "reason": decision["reason"],
            })
            continue
        source = Path(decision["canonical_source"]["path"]).resolve()
        target = Path(decision["target_path"])
        if target.parent.resolve() != library_resolved:
            results.append({"skill_id": decision["skill_id"], "name": decision["name"], "status": "failed", "reason": "目标路径不在统一库直接子目录中"})
            continue
        if target.exists():
            existing = fingerprint(target, [library_resolved], {}, {})
            status = "already_present" if existing and existing[0] == decision["content_hash"] else "blocked"
            reason = "目标中已有相同内容" if status == "already_present" else "目标目录已存在不同内容，未覆盖"
            results.append({"skill_id": decision["skill_id"], "name": decision["name"], "status": status, "reason": reason, "target_path": str(target)})
            continue
        staging = library / f".import-{uuid.uuid4().hex}"
        target_created = False
        try:
            copy_skill(source, staging)
            staged_hash = fingerprint(staging, [library_resolved], {}, {})
            if not staged_hash or staged_hash[0] != decision["content_hash"]:
                raise ValueError("复制后的内容哈希与来源不一致")
            os.replace(staging, target)
            target_created = True
            results.append({"skill_id": decision["skill_id"], "name": decision["name"], "status": "imported", "reason": "复制并校验成功", "source_path": str(source), "target_path": str(target)})
        except Exception as exc:
            if staging.exists():
                shutil.rmtree(staging)
            if target_created and target.exists():
                shutil.rmtree(target)
            results.append({"skill_id": decision["skill_id"], "name": decision["name"], "status": "failed", "reason": str(exc), "source_path": str(source), "target_path": str(target)})

    final_scan = scan()
    final_plan = build_import_plan(final_scan)
    result = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "library_path": str(library),
        "summary": {
            "imported": sum(item["status"] == "imported" for item in results),
            "already_present": sum(item["status"] == "already_present" for item in results),
            "blocked": sum(item["status"] == "blocked" for item in results),
            "skipped": sum(item["status"] == "skipped" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        },
        "results": results,
        "final_plan_summary": final_plan["summary"],
        "safety": {
            "source_files_changed": False,
            "duplicates_removed": False,
            "existing_targets_overwritten": False,
        },
    }
    write_json_atomic(IMPORT_RESULT_PATH, result)
    return result


def scan() -> dict:
    configured = read_config()
    allowed_roots = [item["path"].resolve() for item in configured if item["path"].exists()]
    cache = load_cache()
    next_cache = {}
    roots_result = []
    variants: dict[str, dict] = {}
    fingerprint_cache: dict[str, tuple[str, float] | None] = {}
    errors = []
    for root_info in configured:
        root = root_info["path"]
        root_row = {
            "id": root_info["id"],
            "label": root_info["label"],
            "agents": root_info.get("agents", []),
            "path": str(root),
            "exists": root.is_dir(),
            "skill_count": 0,
        }
        roots_result.append(root_row)
        if not root.is_dir():
            continue
        for skill_path, fallback_name in skill_dirs(root):
            try:
                name, description = read_manifest(skill_path, fallback_name)
                auto_category = classify_skill(name, description)
                auto_description_zh = summarize_skill_zh(name, description, auto_category)
                resolved_path = str(skill_path.resolve())
                if resolved_path not in fingerprint_cache:
                    fingerprint_cache[resolved_path] = fingerprint(skill_path, allowed_roots, cache, next_cache)
                result = fingerprint_cache[resolved_path]
                if result is None:
                    errors.append({"path": str(skill_path), "message": "目录无法读取或链接目标不在已登记的 Skill 根目录内"})
                    continue
                content_hash, mtime = result
                identity = content_hash
                variant = variants.setdefault(identity, {
                    "id": identity[:16],
                    "content_hash": identity,
                    "name": name,
                    "description": description,
                    "auto_category": auto_category,
                    "auto_description_zh": auto_description_zh,
                    "aliases": [],
                    "locations": [],
                    "modified_at": None,
                })
                if name not in variant["aliases"] and name != variant["name"]:
                    variant["aliases"].append(name)
                location = {
                    "root_id": root_info["id"],
                    "root_label": root_info["label"],
                    "agents": root_info.get("agents", []),
                    "path": str(skill_path),
                    "relative_path": str(skill_path.relative_to(root)),
                    "is_symlink": skill_path.is_symlink() or skill_path.absolute() != skill_path.resolve(),
                    "resolved_path": resolved_path,
                }
                if location not in variant["locations"]:
                    variant["locations"].append(location)
                    root_row["skill_count"] += 1
                stamp = datetime.fromtimestamp(mtime, timezone.utc).isoformat() if mtime else None
                if stamp and (variant["modified_at"] is None or stamp > variant["modified_at"]):
                    variant["modified_at"] = stamp
            except Exception as exc:  # 单个异常目录不应中断其余目录扫描
                errors.append({"path": str(skill_path), "message": str(exc)})
    names: dict[str, set[str]] = {}
    for variant in variants.values():
        key = normalized_name(variant["name"])
        names.setdefault(key, set()).add(variant["content_hash"])
    conflicting_names = {key for key, hashes in names.items() if len(hashes) > 1}
    result_variants = []
    for variant in variants.values():
        physical_paths = {os.path.normcase(item["resolved_path"]) for item in variant["locations"]}
        variant["location_count"] = len(variant["locations"])
        variant["physical_copy_count"] = len(physical_paths)
        variant["linked_location_count"] = sum(item["is_symlink"] for item in variant["locations"])
        variant["duplicate_count"] = max(0, len(physical_paths) - 1)
        variant["duplicate"] = variant["duplicate_count"] > 0
        variant["name_conflict"] = len(names.get(normalized_name(variant["name"]), set())) > 1
        variant["name_conflict_group_size"] = len(names.get(normalized_name(variant["name"]), set()))
        result_variants.append(variant)
    result_variants.sort(key=lambda item: (item["name"].casefold(), item["content_hash"]))
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "roots": roots_result,
        "skills": result_variants,
        "summary": {
            "unique_contents": len(result_variants),
            "locations": sum(len(item["locations"]) for item in result_variants),
            "duplicates": sum(item["duplicate"] for item in result_variants),
            "name_conflicts": len(conflicting_names),
        },
        "errors": errors,
    }
    write_json_atomic(CACHE_PATH, {"version": CACHE_VERSION, "entries": next_cache})
    write_json_atomic(REPORT_PATH, payload)
    return payload


def load_notes() -> dict:
    try:
        return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"notes": {}, "categories": {}}


def save_note(name: str, note: str, category: str = "") -> dict:
    data = load_notes()
    if "notes" not in data:
        data["notes"] = {}
    if "categories" not in data:
        data["categories"] = {}
    if note.strip():
        data["notes"][name] = note.strip()
    else:
        data["notes"].pop(name, None)
    if category.strip():
        data["categories"][name] = category.strip()
    else:
        data["categories"].pop(name, None)
    write_json_atomic(NOTES_PATH, data)
    return data


def load_sources() -> dict:
    try:
        data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_sources(sources: dict) -> None:
    write_json_atomic(SOURCES_PATH, sources)


def validate_directory_name(value: str) -> str:
    name = value.strip()
    invalid = set('<>:"/\\|?*')
    device_names = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if not name or name in {".", ".."} or len(name) > 120 or any(char in invalid or ord(char) < 32 for char in name):
        raise ValueError("Skill 目录名包含不支持的字符")
    if name.endswith((".", " ")) or name.split(".", 1)[0].upper() in device_names:
        raise ValueError("Skill 目录名不能以点或空格结尾，也不能使用 Windows 保留名称")
    return name


def discover_skill_sources(root: Path) -> list[tuple[Path, str]]:
    if (root / "SKILL.md").is_file():
        return [(root, ".")]
    found = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name.casefold() not in IGNORED_DIRS and not Path(current, name).is_symlink()]
        if "SKILL.md" not in filenames:
            continue
        skill_path = Path(current)
        if (skill_path / "SKILL.md").is_symlink():
            continue
        relative = skill_path.relative_to(root).as_posix() or "."
        found.append((skill_path, relative))
        directories[:] = []
    return sorted(found, key=lambda item: item[1].casefold())


def library_content_index() -> tuple[Path, dict[str, str], set[str]]:
    library = read_settings()["library_path"]
    hashes = {}
    names = set()
    if not library.is_dir():
        return library, hashes, names
    resolved_library = library.resolve()
    for child in library.iterdir():
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink() or not (child / "SKILL.md").is_file():
            continue
        names.add(normalized_name(child.name))
        result = fingerprint(child, [resolved_library], {}, {})
        if result:
            hashes[result[0]] = child.name
    return library, hashes, names


def preview_candidate_rows(stage_root: Path, meta: dict) -> list[dict]:
    candidates = []
    for skill_path, relative in discover_skill_sources(stage_root):
        result = fingerprint(skill_path, [stage_root.resolve()], {}, {})
        if not result:
            continue
        name, description = read_manifest(skill_path, skill_path.name)
        if not name.strip():
            name = skill_path.name
        candidates.append({
            "candidate_id": str(len(candidates)),
            "name": name.strip(),
            "description": description,
            "content_hash": result[0],
            "relative_path": relative,
        })
        if len(candidates) >= 200:
            break
    if not candidates:
        raise ValueError("未在来源中找到包含 SKILL.md 的技能目录")
    library, known_hashes, known_names = library_content_index()
    for candidate in candidates:
        if candidate["content_hash"] in known_hashes:
            candidate["existing_status"] = "same_content"
            candidate["existing_name"] = known_hashes[candidate["content_hash"]]
        elif normalized_name(candidate["name"]) in known_names:
            candidate["existing_status"] = "name_conflict"
            candidate["existing_name"] = candidate["name"]
        else:
            candidate["existing_status"] = "new"
            candidate["existing_name"] = None
    return candidates


def register_source_preview(kind: str, stage_root: Path, source: dict) -> dict:
    prune_source_previews()
    candidates = preview_candidate_rows(stage_root, source)
    token = uuid.uuid4().hex
    SOURCE_PREVIEWS[token] = {
        "created_at": time.time(),
        "kind": kind,
        "stage_root": str(stage_root),
        "source": source,
        "candidates": {item["candidate_id"]: item for item in candidates},
    }
    return {"preview_id": token, "source": source, "candidates": candidates}


def cleanup_source_previews() -> None:
    for item in SOURCE_PREVIEWS.values():
        stage_root = Path(item["stage_root"])
        if inside(stage_root.resolve(strict=False), STAGING_DIR.resolve(strict=False)) and stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
    SOURCE_PREVIEWS.clear()


def prune_source_previews() -> None:
    now = time.time()
    for token, item in list(SOURCE_PREVIEWS.items()):
        if now - item["created_at"] <= PREVIEW_TTL_SECONDS:
            continue
        stage_root = Path(item["stage_root"])
        if inside(stage_root.resolve(strict=False), STAGING_DIR.resolve(strict=False)) and stage_root.exists():
            shutil.rmtree(stage_root, ignore_errors=True)
        SOURCE_PREVIEWS.pop(token, None)


def validate_git_url(value: str) -> str:
    url = value.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.query or parsed.fragment:
        raise ValueError("目前只接受不含帳密的 HTTPS Git 仓库地址")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")) or "." not in host:
        raise ValueError("Git 地址必须指向公开 HTTPS 主机")
    import ipaddress
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Git 地址不能指向本机或私有网络")
    if not parsed.path or parsed.path == "/":
        raise ValueError("Git 地址缺少仓库路径")
    return url


def validate_git_branch(value: str) -> str:
    branch = value.strip()
    if branch and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) or ".." in branch or "@{" in branch or branch.endswith((".", "/"))):
        raise ValueError("Git 分支名称不合法")
    return branch


def parse_github_source_url(value: str) -> tuple[str, str, str]:
    url = validate_git_url(value)
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if (parsed.hostname or "").casefold() not in {"github.com", "www.github.com"} or len(parts) < 2:
        raise ValueError("请输入 GitHub 仓库或 Skill 地址，例如 https://github.com/owner/repository")
    owner, repository = parts[:2]
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("GitHub 仓库地址不完整")
    inferred_branch = ""
    inferred_path = ""
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
            raise ValueError("GitHub 链接应为仓库地址或 tree/blob 技能路径")
        inferred_branch = parts[3]
        path_parts = parts[4:]
        if parts[2] == "blob" and path_parts and path_parts[-1].casefold() == "skill.md":
            path_parts.pop()
        inferred_path = "/".join(path_parts)
    return f"https://github.com/{owner}/{repository}", inferred_branch, inferred_path


def validate_skill_relative_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized in {"", ".", "./"}:
        return "."
    relative = PurePosixPath(normalized)
    if relative.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in relative.parts):
        raise ValueError("Skill 相对路径不合法")
    return relative.as_posix()


def find_existing_skill_in_repository(repo: Path, directory: str, expected_hash: str, relative_path: str, explicit_path: bool = False) -> tuple[Path, str, str]:
    if relative_path != "." or explicit_path:
        candidate_path = repo if relative_path == "." else repo.joinpath(*PurePosixPath(relative_path).parts)
        candidates = [(candidate_path, relative_path)]
    else:
        candidates = discover_skill_sources(repo)
        if len(candidates) > 200:
            raise ValueError("仓库中的 Skill 超过 200 个，请填写 Skill 的仓库相对路径")
        named = []
        matching = []
        for path, relative in candidates:
            manifest_name, _ = read_manifest(path, path.name)
            if normalized_name(path.name) == normalized_name(directory) or normalized_name(manifest_name) == normalized_name(directory):
                named.append((path, relative))
            result = fingerprint(path, [repo.resolve()], {}, {})
            if result and result[0] == expected_hash:
                matching.append((path, relative, result[0]))
        if len(matching) == 1:
            return matching[0]
        if len(named) == 1:
            chosen_path, chosen_relative = named[0]
            result = fingerprint(chosen_path, [repo.resolve()], {}, {})
            if result:
                return chosen_path, chosen_relative, result[0]
        if len(candidates) == 1 and explicit_path:
            chosen_path, chosen_relative = candidates[0]
            result = fingerprint(chosen_path, [repo.resolve()], {}, {})
            if result:
                return chosen_path, chosen_relative, result[0]
        raise ValueError("无法唯一匹配仓库中的 Skill，请确认 Skill 名称，或填写仓库内相对路径（仓库根目录填写 .）")

    candidate_path = candidates[0][0]
    resolved_repo = repo.resolve()
    manifest = candidate_path / "SKILL.md"
    if (not inside(candidate_path.resolve(strict=False), resolved_repo)
            or is_reparse_point(candidate_path)
            or is_reparse_point(manifest)
            or not manifest.is_file()):
        raise ValueError("指定的仓库路径中没有有效的 SKILL.md")
    result = fingerprint(candidate_path, [resolved_repo], {}, {})
    if not result:
        raise ValueError("无法读取 GitHub 仓库中的 Skill")
    return candidate_path, relative_path, result[0]


def preview_existing_git_association(directory: str, repository_url: str, branch: str, relative_path: str) -> dict:
    prune_source_previews()
    name = validate_directory_name(directory)
    repository_url, url_branch, url_path = parse_github_source_url(repository_url)
    branch = validate_git_branch(branch or url_branch)
    explicit_path = bool(relative_path.strip()) or bool(url_path)
    relative_path = validate_skill_relative_path(relative_path or url_path)
    library = read_settings()["library_path"]
    target = library / name
    if library.exists() and (not library.is_dir() or library.is_symlink()):
        raise ValueError("统一库必须是普通目录")
    if is_reparse_point(target) or not target.is_dir() or target.parent.resolve() != library.resolve():
        raise FileNotFoundError("统一库中找不到可关联的物理 Skill")
    current = fingerprint(target, [library.resolve()], {}, {})
    if not current:
        raise ValueError("无法读取统一库中的 Skill")

    token = uuid.uuid4().hex
    stage_root = STAGING_DIR / f"association-{token}"
    stage_root.mkdir(parents=True, exist_ok=False)
    try:
        repo = stage_root / "repo"
        clone_git_repository(repository_url, branch, repo)
        candidate, selected_relative, latest_hash = find_existing_skill_in_repository(
            repo, name, current[0], relative_path, explicit_path
        )
        local_changes = current[0] != latest_hash
        source = {
            "type": "git",
            "platform": "github",
            "repository_url": repository_url,
            "branch": branch,
            "relative_path": selected_relative,
            # 若内容不同，以远端指纹作为基线，让更新检查识别并保护本地改写。
            "installed_hash": latest_hash if local_changes else current[0],
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        SOURCE_PREVIEWS[token] = {
            "created_at": time.time(),
            "kind": "git_association",
            "stage_root": str(stage_root),
            "directory": name,
            "candidate_path": str(candidate),
            "source": source,
            "current_hash": current[0],
            "latest_hash": latest_hash,
            "local_changes": local_changes,
        }
        return {
            "preview_id": token,
            "directory": name,
            "source": source,
            "current_hash": current[0],
            "latest_hash": latest_hash,
            "local_changes": local_changes,
        }
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def apply_existing_git_association(preview_id: str) -> dict:
    prune_source_previews()
    preview = SOURCE_PREVIEWS.get(preview_id)
    if not preview or preview.get("kind") != "git_association":
        raise ValueError("GitHub 来源预览已过期，请重新检查")
    name = preview["directory"]
    library = read_settings()["library_path"]
    target = library / name
    stage_root = Path(preview["stage_root"]).resolve()
    candidate = Path(preview["candidate_path"]).resolve()
    if not inside(candidate, stage_root) or not (candidate / "SKILL.md").is_file():
        raise ValueError("GitHub 来源预览内容已失效")
    current = fingerprint(target, [library.resolve()], {}, {})
    latest = fingerprint(candidate, [stage_root], {}, {})
    if not current or current[0] != preview["current_hash"] or not latest or latest[0] != preview["latest_hash"]:
        raise ValueError("检查后本地或 GitHub 内容发生变化，请重新检查")
    sources = load_sources()
    sources[name] = preview["source"]
    save_sources(sources)
    SOURCE_PREVIEWS.pop(preview_id, None)
    shutil.rmtree(stage_root, ignore_errors=True)
    return {"directory": name, "source": preview["source"], "local_changes": preview["local_changes"]}


def clone_git_repository(url: str, branch: str, destination: Path) -> None:
    if shutil.which("git") is None:
        raise ValueError("尚未安装 Git，无法下载仓库")
    command = ["git", "clone", "--depth=1", "--no-tags"]
    if branch:
        command.extend(["--single-branch", "--branch", branch])
    command.extend([url, str(destination)])
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False, env=environment)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git 下载超时（5 分钟），请检查仓库大小或网络") from exc
    if result.returncode:
        raise ValueError("Git 克隆失败，请检查仓库地址、分支、网络权限和 Git 安装")


def preview_git_source(repository_url: str, branch: str) -> dict:
    stage_root = STAGING_DIR / f"source-{uuid.uuid4().hex}"
    stage_root.mkdir(parents=True, exist_ok=False)
    try:
        clone_git_repository(repository_url, branch, stage_root)
        return register_source_preview("git", stage_root, {"type": "git", "repository_url": repository_url, "branch": branch})
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def extract_skill_archive(payload: bytes, display_name: str) -> dict:
    if not payload or len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError("压缩包必须小于 100 MB")
    token = uuid.uuid4().hex
    stage_root = STAGING_DIR / f"source-{token}"
    stage_root.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES or sum(info.file_size for info in infos) > MAX_ARCHIVE_UNPACKED_BYTES:
                raise ValueError("压缩包文件数或解压体积超过安全限制")
            for info in infos:
                normalized = info.filename.replace("\\", "/")
                relative = PurePosixPath(normalized)
                if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts) or ":" in relative.parts[0]:
                    raise ValueError("压缩包包含不安全的文件路径")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ValueError("压缩包包含符号链接，已拒绝导入")
                target = stage_root.joinpath(*relative.parts)
                if not inside(target.resolve(strict=False), stage_root.resolve()):
                    raise ValueError("压缩包文件路径超出暂存目录")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        return register_source_preview("archive", stage_root, {"type": "archive", "file_name": display_name})
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def apply_source_preview(preview_id: str, selected: list) -> dict:
    prune_source_previews()
    preview = SOURCE_PREVIEWS.get(preview_id)
    if not preview:
        raise ValueError("导入预览已过期，请重新选择来源")
    if not isinstance(selected, list) or not selected or len(selected) > 200:
        raise ValueError("至少选择一个要导入的 Skill")
    stage_root = Path(preview["stage_root"]).resolve()
    library = read_settings()["library_path"]
    if library.exists() and (not library.is_dir() or library.is_symlink()):
        raise ValueError("统一库必须是普通目录")
    library.mkdir(parents=True, exist_ok=True)
    library_resolved = library.resolve()
    source_info = preview["source"]
    results = []
    batch_names = set()
    known_hashes = library_content_index()[1]
    sources = load_sources()
    for selection in selected:
        if not isinstance(selection, dict):
            results.append({"status": "failed", "reason": "选择项格式不正确"})
            continue
        candidate = preview["candidates"].get(str(selection.get("candidate_id", "")))
        if not candidate:
            results.append({"status": "failed", "reason": "来源预览中的技能不存在"})
            continue
        try:
            target_name = validate_directory_name(str(selection.get("target_name") or candidate["name"]))
        except ValueError as exc:
            results.append({"name": candidate["name"], "status": "failed", "reason": str(exc)})
            continue
        normalized = normalized_name(target_name)
        if normalized in batch_names:
            results.append({"name": target_name, "status": "conflict", "reason": "本次选择中存在重复目录名"})
            continue
        batch_names.add(normalized)
        relative = candidate["relative_path"]
        source_path = stage_root if relative == "." else (stage_root / Path(relative))
        source_path = source_path.resolve()
        if not inside(source_path, stage_root) or not (source_path / "SKILL.md").is_file():
            results.append({"name": target_name, "status": "failed", "reason": "来源目录已失效或缺少 SKILL.md"})
            continue
        current = fingerprint(source_path, [stage_root], {}, {})
        if not current or current[0] != candidate["content_hash"]:
            results.append({"name": target_name, "status": "failed", "reason": "预览后来源内容发生变化"})
            continue
        target = library / target_name
        if target.exists() or target.is_symlink():
            existing = fingerprint(target, [library_resolved], {}, {})
            status = "already_present" if existing and existing[0] == candidate["content_hash"] else "conflict"
            results.append({"name": target_name, "status": status, "reason": "目标目录已有相同内容" if status == "already_present" else "目标目录已有不同内容，未覆盖"})
            continue
        duplicate = known_hashes.get(candidate["content_hash"])
        if duplicate:
            results.append({"name": target_name, "status": "already_present", "reason": f"内容已由 {duplicate} 管理"})
            continue
        temporary = library / f".import-{uuid.uuid4().hex}"
        try:
            copy_skill(source_path, temporary)
            copied = fingerprint(temporary, [library_resolved], {}, {})
            if not copied or copied[0] != candidate["content_hash"]:
                raise ValueError("复制后内容哈希不匹配")
            os.replace(temporary, target)
            source_record = {**source_info, "relative_path": relative, "installed_hash": copied[0], "installed_at": datetime.now(timezone.utc).isoformat()}
            sources[target_name] = source_record
            known_hashes[copied[0]] = target_name
            results.append({"name": target_name, "status": "imported", "reason": "复制并校验成功", "source": source_record})
        except Exception as exc:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            results.append({"name": target_name, "status": "failed", "reason": str(exc)})
    save_sources(sources)
    SOURCE_PREVIEWS.pop(preview_id, None)
    shutil.rmtree(stage_root, ignore_errors=True)
    return {
        "summary": {
            "imported": sum(item["status"] == "imported" for item in results),
            "already_present": sum(item["status"] == "already_present" for item in results),
            "conflict": sum(item["status"] == "conflict" for item in results),
            "failed": sum(item["status"] == "failed" for item in results),
        },
        "results": results,
    }


def create_local_skill(name: str, description: str, instructions: str) -> dict:
    directory_name = validate_directory_name(name)
    description = description.strip()
    instructions = instructions.strip()
    if not description or len(description) > 1000:
        raise ValueError("用途说明必须填写且不超过 1000 个字符")
    if len(instructions) > 65536 or "\x00" in instructions:
        raise ValueError("技能说明过长或包含无效字符")
    library = read_settings()["library_path"]
    if library.exists() and (not library.is_dir() or library.is_symlink()):
        raise ValueError("统一库必须是普通目录")
    library.mkdir(parents=True, exist_ok=True)
    target = library / directory_name
    if target.exists() or target.is_symlink():
        raise ValueError("统一库中已存在相同目录名的技能")
    manifest = f"---\nname: {json.dumps(directory_name, ensure_ascii=False)}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{instructions}\n"
    temporary = library / f".create-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        (temporary / "SKILL.md").write_text(manifest, encoding="utf-8")
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    result = fingerprint(target, [library.resolve()], {}, {})
    sources = load_sources()
    sources[directory_name] = {"type": "local", "created_at": datetime.now(timezone.utc).isoformat(), "installed_hash": result[0] if result else None}
    save_sources(sources)
    return {"name": directory_name, "path": str(target), "content_hash": result[0] if result else None}


def check_git_update(directory: str) -> dict:
    prune_source_previews()
    name = validate_directory_name(directory)
    sources = load_sources()
    source = sources.get(name)
    if not source or source.get("type") != "git":
        raise ValueError("该 Skill 没有可检查的 Git 来源记录")
    library = read_settings()["library_path"]
    target = library / name
    current = fingerprint(target, [library.resolve()], {}, {})
    if not current:
        raise FileNotFoundError("统一库中找不到可更新的 Skill")
    token = uuid.uuid4().hex
    stage_root = STAGING_DIR / f"update-{token}"
    stage_root.mkdir(parents=True, exist_ok=False)
    try:
        clone_git_repository(validate_git_url(source["repository_url"]), validate_git_branch(source.get("branch", "")), stage_root / "repo")
        repo = stage_root / "repo"
        relative = source.get("relative_path", ".")
        latest = repo if relative == "." else (repo / Path(relative))
        if not inside(latest.resolve(), repo.resolve()) or not (latest / "SKILL.md").is_file():
            raise ValueError("Git 来源中已找不到原 Skill 路径")
        latest_hash = fingerprint(latest, [repo.resolve()], {}, {})
        if not latest_hash:
            raise ValueError("无法读取 Git 最新版本")
        if latest_hash[0] == current[0]:
            shutil.rmtree(stage_root, ignore_errors=True)
            return {"status": "up_to_date", "directory": name, "current_hash": current[0], "latest_hash": latest_hash[0]}
        local_changes = current[0] != source.get("installed_hash")
        SOURCE_PREVIEWS[token] = {
            "created_at": time.time(),
            "kind": "git_update",
            "stage_root": str(stage_root),
            "source": source,
            "directory": name,
            "candidate_path": str(latest),
            "current_hash": current[0],
            "latest_hash": latest_hash[0],
            "local_changes": local_changes,
        }
        latest_name, latest_description = read_manifest(latest, name)
        return {
            "status": "update_available",
            "preview_id": token,
            "directory": name,
            "current_hash": current[0],
            "latest_hash": latest_hash[0],
            "latest_name": latest_name,
            "latest_description": latest_description,
            "local_changes": local_changes,
            "can_apply": not local_changes,
        }
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def apply_git_update(preview_id: str) -> dict:
    prune_source_previews()
    preview = SOURCE_PREVIEWS.get(preview_id)
    if not preview or preview.get("kind") != "git_update":
        raise ValueError("更新预览已过期，请重新检查")
    if preview["local_changes"]:
        raise ValueError("统一库版本包含本地改动；为避免覆盖，不能自动更新")
    name = preview["directory"]
    library = read_settings()["library_path"]
    target = library / name
    latest = Path(preview["candidate_path"]).resolve()
    stage_root = Path(preview["stage_root"]).resolve()
    if not inside(latest, stage_root) or not (latest / "SKILL.md").is_file():
        raise ValueError("更新暂存内容已失效")
    current = fingerprint(target, [library.resolve()], {}, {})
    new_hash = fingerprint(latest, [stage_root], {}, {})
    if not current or current[0] != preview["current_hash"] or not new_hash or new_hash[0] != preview["latest_hash"]:
        raise ValueError("检查后本地或远程内容发生变化，请重新检查")
    backup = library.parent / "backups" / f"update-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}" / name
    backup.parent.mkdir(parents=True, exist_ok=True)
    temporary = library / f".update-{uuid.uuid4().hex}"
    moved_old = False
    try:
        copy_skill(latest, temporary)
        copied = fingerprint(temporary, [library.resolve()], {}, {})
        if not copied or copied[0] != new_hash[0]:
            raise ValueError("新版本哈希校验失败")
        os.replace(target, backup)
        moved_old = True
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        if moved_old and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    sources = load_sources()
    source = dict(sources.get(name, {}))
    source["installed_hash"] = new_hash[0]
    source["installed_at"] = datetime.now(timezone.utc).isoformat()
    sources[name] = source
    save_sources(sources)
    SOURCE_PREVIEWS.pop(preview_id, None)
    shutil.rmtree(stage_root, ignore_errors=True)
    return {"name": name, "content_hash": new_hash[0], "backup_path": str(backup)}


class Handler(BaseHTTPRequestHandler):
    server_version = "SkillShelfLocal/0.2"

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_request(self, maximum: int = 65536) -> dict:
        content_type = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求长度无效") from exc
        if not content_type.startswith("application/json") or length <= 0 or length > maximum:
            raise ValueError("请求类型或大小无效")
        try:
            request = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 格式无效") from exc
        if not isinstance(request, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return request

    def origin_allowed(self) -> bool:
        host_header = self.headers.get("Host", "")
        host = urlparse("//" + host_header).hostname
        if host not in {"127.0.0.1", "localhost"}:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == self.server.server_port

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/health":
            self.send_json(200, {"ok": True, "service": self.server_version})
        elif route == "/api/roots":
            data = [{"id": item["id"], "label": item["label"], "agents": item.get("agents", []), "path": str(item["path"]), "exists": item["path"].is_dir()} for item in read_config()]
            self.send_json(200, {"roots": data})
        elif route == "/api/skills":
            self.send_json(200, load_latest_scan())
        elif route == "/api/conflicts":
            self.send_json(200, build_conflict_overview(load_latest_scan()))
        elif route == "/api/import-plan":
            self.send_json(200, build_import_plan(load_latest_scan()))
        elif route == "/api/deploy-plan":
            self.send_json(200, build_deploy_plan(load_latest_scan()))
        elif route == "/api/deploy-status":
            self.send_json(200, build_deploy_status(load_latest_scan()))
        elif route == "/api/reconcile-status":
            try:
                self.send_json(200, json.loads(RECONCILE_RESULT_PATH.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                self.send_json(200, {"summary": {"linked": 0, "skipped": 0, "failed": 0}, "results": []})
        elif route == "/api/skills/notes":
            self.send_json(200, load_notes())
        elif route == "/api/sources":
            self.send_json(200, {"sources": load_sources()})
        elif route == "/":
            page = (RESOURCE_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if not self.origin_allowed():
            self.send_json(403, {"error": "仅允许本机同源请求"})
            return
        if route == "/api/conflicts/preview":
            try:
                request = self.read_json_request(16384)
                name = request.get("name")
                expected_hashes = request.get("expected_hashes")
                if not isinstance(name, str) or not isinstance(expected_hashes, list):
                    raise ValueError("冲突名称或版本列表无效")
                with SCAN_LOCK:
                    payload = build_conflict_merge_preview(load_latest_scan(), name, expected_hashes)
                self.send_json(200, payload)
            except (ValueError, OSError) as exc:
                self.send_json(409, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "融合方案预览失败"})
        elif route == "/api/conflicts/resolve":
            try:
                request = self.read_json_request(2 * 1024 * 1024)
                with SCAN_LOCK:
                    payload = apply_conflict_resolution(request)
                payload["conflicts"] = build_conflict_overview(payload["scan"])
                self.send_json(200, payload)
            except (ValueError, FileNotFoundError, OSError) as exc:
                self.send_json(409, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "同名冲突统一失败"})
        elif route == "/api/sources/git/preview":
            try:
                request = self.read_json_request(16384)
                repository_url = validate_git_url(str(request.get("repository_url", "")))
                branch = validate_git_branch(str(request.get("branch", "")))
                payload = preview_git_source(repository_url, branch)
                self.send_json(200, payload)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "Git 来源预览失败"})
        elif route == "/api/sources/git/association/preview":
            try:
                request = self.read_json_request(16384)
                payload = preview_existing_git_association(
                    str(request.get("directory", "")),
                    str(request.get("repository_url", "")),
                    str(request.get("branch", "")),
                    str(request.get("relative_path", "")),
                )
                self.send_json(200, payload)
            except (ValueError, FileNotFoundError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "GitHub 来源检查失败"})
        elif route == "/api/sources/git/association/apply":
            try:
                request = self.read_json_request(4096)
                if request.get("confirm") != "REGISTER_GIT_SOURCE":
                    self.send_json(409, {"error": "确认值无效"})
                    return
                with SCAN_LOCK:
                    payload = apply_existing_git_association(str(request.get("preview_id", "")))
                self.send_json(200, payload)
            except (ValueError, FileNotFoundError, OSError) as exc:
                self.send_json(409, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "GitHub 来源登记失败"})
        elif route == "/api/sources/archive/preview":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not self.headers.get("Content-Type", "").startswith(("application/zip", "application/octet-stream")):
                    raise ValueError("压缩包请求类型无效")
                if length <= 0 or length > MAX_ARCHIVE_BYTES:
                    raise ValueError("压缩包必须小于 100 MB")
                display_name = unquote(self.headers.get("X-File-Name", "skill.zip")).replace("\\", "/").rsplit("/", 1)[-1]
                if not display_name.casefold().endswith((".zip", ".skill")):
                    raise ValueError("请选择 ZIP 或 .skill 压缩包")
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("压缩包上传不完整")
                self.send_json(200, extract_skill_archive(payload, display_name))
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                self.send_json(400, {"error": str(exc) or "压缩包无效"})
            except Exception:
                self.send_json(500, {"error": "压缩包预览失败"})
        elif route == "/api/sources/create":
            try:
                request = self.read_json_request(100000)
                with SCAN_LOCK:
                    payload = create_local_skill(str(request.get("name", "")), str(request.get("description", "")), str(request.get("instructions", "")))
                    payload["reconcile_summary"] = reconcile_missing_links().get("summary", {})
                    scan_data = load_latest_scan()
                self.send_json(200, {**payload, "scan": scan_data})
            except (ValueError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "本地 Skill 创建失败"})
        elif route == "/api/sources/apply":
            try:
                request = self.read_json_request(1024 * 1024)
                if request.get("confirm") != "IMPORT_SOURCE_SKILLS":
                    self.send_json(409, {"error": "确认值无效"})
                    return
                with SCAN_LOCK:
                    payload = apply_source_preview(str(request.get("preview_id", "")), request.get("selected"))
                    payload["reconcile_summary"] = reconcile_missing_links().get("summary", {})
                    scan_data = load_latest_scan()
                payload["scan"] = scan_data
                self.send_json(200, payload)
            except (ValueError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "来源导入失败"})
        elif route == "/api/skills/update/check":
            try:
                request = self.read_json_request(4096)
                with SCAN_LOCK:
                    self.send_json(200, check_git_update(str(request.get("directory", ""))))
            except (ValueError, FileNotFoundError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "Git 更新检查失败"})
        elif route == "/api/skills/update/apply":
            try:
                request = self.read_json_request(4096)
                if request.get("confirm") != "APPLY_GIT_UPDATE":
                    self.send_json(409, {"error": "确认值无效"})
                    return
                with SCAN_LOCK:
                    payload = apply_git_update(str(request.get("preview_id", "")))
                    payload["reconcile_summary"] = reconcile_missing_links().get("summary", {})
                    scan_data = load_latest_scan()
                payload["scan"] = scan_data
                self.send_json(200, payload)
            except (ValueError, FileNotFoundError, OSError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "Git 更新失败"})
        elif route == "/api/scan":
            with SCAN_LOCK:
                self.send_json(200, scan())
        elif route == "/api/import-plan":
            self.send_json(200, build_import_plan(load_latest_scan()))
        elif route == "/api/deploy-plan":
            self.send_json(200, build_deploy_plan(load_latest_scan()))
        elif route == "/api/import/apply":
            content_type = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not content_type.startswith("application/json") or length <= 0 or length > 1024:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self.send_json(400, {"error": "invalid_json"})
                return
            if request.get("confirm") != "IMPORT_TO_LIBRARY":
                self.send_json(409, {"error": "confirmation_required", "expected": "IMPORT_TO_LIBRARY"})
                return
            with SCAN_LOCK:
                self.send_json(200, apply_import_plan())
        elif route == "/api/deploy/apply":
            content_type = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not content_type.startswith("application/json") or length <= 0 or length > 1024:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self.send_json(400, {"error": "invalid_json"})
                return
            if request.get("confirm") != "DEPLOY_AGENT_LINKS":
                self.send_json(409, {"error": "confirmation_required", "expected": "DEPLOY_AGENT_LINKS"})
                return
            with SCAN_LOCK:
                self.send_json(200, apply_deploy_plan())
        elif route == "/api/reconcile-links":
            with SCAN_LOCK:
                self.send_json(200, reconcile_missing_links())
        elif route in {"/api/skills/refresh", "/api/skills/delete"}:
            content_type = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not content_type.startswith("application/json") or length <= 0 or length > 4096:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self.send_json(400, {"error": "invalid_json"})
                return
            if not isinstance(request, dict):
                self.send_json(400, {"error": "invalid_request"})
                return
            directory = request.get("directory")
            if not isinstance(directory, str) or not directory:
                self.send_json(400, {"error": "directory_required"})
                return
            if route == "/api/skills/delete" and request.get("confirm") != "MOVE_SKILL_TO_RECYCLE_BIN":
                self.send_json(409, {"error": "confirmation_required", "expected": "MOVE_SKILL_TO_RECYCLE_BIN"})
                return
            try:
                with SCAN_LOCK:
                    payload = refresh_skill(directory) if route.endswith("refresh") else delete_skill_to_recycle_bin(directory, request.get("expected_hash"))
                self.send_json(200, payload)
            except (ValueError, FileNotFoundError) as exc:
                self.send_json(409, {"error": str(exc)})
        elif route == "/api/skills/notes":
            content_type = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not content_type.startswith("application/json") or length <= 0 or length > 65536:
                self.send_json(400, {"error": "invalid_request"})
                return
            try:
                request = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self.send_json(400, {"error": "invalid_json"})
                return
            name = request.get("name")
            if not isinstance(name, str) or not name:
                self.send_json(400, {"error": "name_required"})
                return
            note = str(request.get("note", ""))
            category = str(request.get("category", ""))
            with SCAN_LOCK:
                updated = save_note(name, note, category)
            self.send_json(200, {"ok": True, "notes": updated.get("notes", {}), "categories": updated.get("categories", {})})
        else:
            self.send_json(404, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def create_server(port: int = PORT, start_watcher: bool = True) -> ThreadingHTTPServer:
    initialize_runtime_data()
    http_server = ThreadingHTTPServer((HOST, port), Handler)
    if start_watcher and read_settings()["auto_link_new_skills"]:
        threading.Thread(target=watch_library, daemon=True, name="skill-shelf-watcher").start()
    return http_server


def main() -> None:
    http_server = create_server()
    print(f"Skill Shelf 本地扫描 API：http://{HOST}:{http_server.server_port}")
    print("只扫描 roots.json 中登记的目录；不会执行 Skill 内的脚本。")
    try:
        http_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http_server.shutdown()
        http_server.server_close()
        cleanup_source_previews()


if __name__ == "__main__":
    main()
