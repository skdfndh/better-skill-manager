"""在临时目录应用局部补丁，只提取初始化方法执行离线替身检查。"""

import ast
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SOURCE_SHA256 = "bef2630a2a688ceba112faeae366711bd637dc08d0a0131a22d971a1af523883"
PATCHED_SHA256 = "731282ec6bb0047b211ea16ea5116503178e486a572403c0c3debaeca0c77ebd"


def exercise(path, engine, fail_rapid=False, fail_layout=False):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PDFOCRProcessor")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_init_engine")
    module = ast.Module(body=[ast.ClassDef(name="Probe", bases=[], keywords=[], body=[method], decorator_list=[])], type_ignores=[])
    calls = []

    def rapid():
        calls.append("rapid")
        if fail_rapid:
            raise RuntimeError("合成本地初始化失败")
        return object()

    def layout():
        calls.append("layout")
        if fail_layout:
            raise RuntimeError("合成版面引擎初始化失败")
        return object()

    def cloud():
        calls.append("cloud")
        return object()

    scope = {"RapidOCREngine": rapid, "RapidDocEngine": layout, "PaddleOCREngine": layout, "SiliconFlowOCREngine": cloud}
    exec(compile(ast.fix_missing_locations(module), "isolated_ocr_probe", "exec"), scope)
    probe = scope["Probe"]()
    probe.engine_type = engine
    error = None
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            probe._init_engine()
        except (RuntimeError, ValueError) as exc:
            error = type(exc).__name__
    return calls, error, probe.engine_type


def main():
    if len(sys.argv) not in {2, 3}:
        raise SystemExit("用法：verify_ocr_patch.py <明确提供的原版源文件> [已修改文件]")
    original = Path(sys.argv[1]).resolve()
    content = original.read_text(encoding="utf-8-sig")
    if hashlib.sha256(content.encode()).hexdigest() != SOURCE_SHA256:
        raise SystemExit("源文件与已审阅版本不同，停止执行初始化方法；请先审阅新版本。")
    with tempfile.TemporaryDirectory(prefix="ocr-patch-check-") as workspace:
        target = Path(workspace) / "scripts/pdf_ocr_processor.py"
        target.parent.mkdir()
        target.write_text(content, encoding="utf-8", newline="\n")
        assert exercise(target, "rapid", fail_rapid=True) == (["rapid", "cloud"], None, "siliconflow")
        patch = REPO / "patches/pdf-ocr-stop-implicit-cloud.patch"
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=workspace, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=workspace, check=True)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == PATCHED_SHA256, "补丁结果与已审阅内容不同"
        cases = [
            ("rapid", True, False, ["rapid"], "RuntimeError", "rapid"),
            ("rapid", False, False, ["rapid"], None, "rapid"),
            ("siliconflow", False, False, ["cloud"], None, "siliconflow"),
            ("rapidoc", False, True, ["layout", "rapid"], None, "rapid"),
            ("paddle", False, True, ["layout", "rapid"], None, "rapid"),
            ("unknown", False, False, [], "ValueError", "unknown"),
        ]
        for engine, fail_rapid, fail_layout, calls, error, final_engine in cases:
            assert exercise(target, engine, fail_rapid, fail_layout) == (calls, error, final_engine)
        if len(sys.argv) == 3:
            assert target.read_text(encoding="utf-8") == Path(sys.argv[2]).read_text(encoding="utf-8-sig"), "补丁结果与指定改造文件不一致"
        result = {"patch_applies": True, "offline_initialization_checks": 6, "source_sha256_normalized": hashlib.sha256(content.encode()).hexdigest(), "patched_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "real_ocr": "未验证"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
