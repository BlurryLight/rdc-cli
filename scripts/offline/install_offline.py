#!/usr/bin/env python3
"""Offline installer for rdc-cli + prebuilt RenderDoc module (Windows x64, CPython 3.12).

Everything needed is inside this folder:

    wheels/                 rdc-cli 0.7.0 + click + numpy + Pillow (cp312 / win_amd64)
    payload/win-x64/        renderdoc.pyd / renderdoc.dll / renderdoccmd.exe / VC runtime / Android APKs
    requirements.txt        exact pins used by wheels/

No venv, no uv, no Visual Studio, no internet. The script is idempotent:
re-running only copies files whose sha256 differs and lets pip no-op.

Usage:
    python install_offline.py                 # install into the running Python 3.12
    python install_offline.py --check         # verify only, no writes
    python install_offline.py --force         # recopy payload unconditionally
    python install_offline.py --no-pip        # payload only, skip rdc-cli install
    python install_offline.py --python PATH   # re-exec with another 3.12 interpreter
    python install_offline.py --pip-args "-i http://mirror/simple"   # extra pip args

Exit codes: 0 = ok, 1 = failure/drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
REQUIRED_PY = (3, 12)  # renderdoc.pyd is a cp312 native extension; do not relax
BUNDLE = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(f"[rdc] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[rdc] WARN: {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"[rdc] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def module_dir() -> Path:
    return local_appdata() / "rdc" / "renderdoc"


def apk_dir() -> Path:
    return local_appdata() / "rdc" / "share" / "renderdoc" / "plugins" / "android"


# ---------------------------------------------------------------------------
# python / pip
# ---------------------------------------------------------------------------
def maybe_reexec(args: argparse.Namespace) -> None:
    """Allow --python to point at a different 3.12 interpreter."""
    if not args.python:
        return
    target = Path(args.python)
    if not target.is_file():
        die(f"--python 指向的文件不存在：{target}")
    if target.resolve() == Path(sys.executable).resolve():
        return
    cmd = [str(target), str(Path(__file__).resolve())]
    cmd += [a for a in sys.argv[1:] if a not in ("--python", args.python)]
    raise SystemExit(subprocess.run(cmd).returncode)


def check_python() -> None:
    if not IS_WINDOWS:
        die("此包只支持 Windows x64（payload 为 win-x64 原生扩展）")
    if sys.version_info[:2] != REQUIRED_PY:
        die(
            f"需要 CPython {REQUIRED_PY[0]}.{REQUIRED_PY[1]}，当前是 "
            f"{sys.version_info.major}.{sys.version_info.minor}（{sys.executable}）。\n"
            f"  renderdoc.pyd 是 cp312 ABI 的原生扩展，换 Python 版本必须重新编译，"
            f"不能靠改环境变量绕过。\n"
            f"  装好 3.12 后重跑：python install_offline.py --python C:\\Path\\to\\python3.12\\python.exe"
        )
    if sys.maxsize <= 2**32:
        die("需要 64 位 Python（payload 为 x64）")


def run_pip(check: bool, extra: str) -> int:
    base = [sys.executable, "-m", "pip", "install", "--no-index", "--no-warn-script-location",
            "--find-links", str(BUNDLE / "wheels"), "-r", str(BUNDLE / "requirements.txt")]
    if extra:
        base += extra.split()
    if check:
        base += ["--dry-run"]
    result = subprocess.run(base)
    if result.returncode != 0 and not check:
        warn("pip 安装失败，尝试加 --user 重试（无管理员权限的常见情况）")
        result = subprocess.run(base + ["--user"])
    return result.returncode


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def load_manifest() -> dict:
    path = BUNDLE / "payload" / "win-x64" / "MANIFEST.json"
    if not path.is_file():
        die(f"缺少 payload：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def dest_for(rel: str) -> Path | None:
    if rel.startswith("renderdoc/"):
        return module_dir() / Path(rel).name
    if rel.startswith("android/"):
        return apk_dir() / Path(rel).name
    return None


def verify_source(manifest: dict) -> None:
    root = BUNDLE / "payload" / "win-x64"
    for rel, meta in manifest.get("files", {}).items():
        src = root / rel
        if not src.is_file():
            die(f"payload 文件缺失：{src}")
        if src.stat().st_size != meta["size"]:
            die(f"payload 大小不符：{src}（期望 {meta['size']}）")
        actual = sha256(src)
        if actual != meta["sha256"]:
            die(f"payload 校验失败：{src}\n  期望 {meta['sha256']}\n  实际 {actual}")


def install_payload(manifest: dict, force: bool, check: bool) -> int:
    root = BUNDLE / "payload" / "win-x64"
    changed = 0
    for rel, meta in manifest.get("files", {}).items():
        src = root / rel
        dst = dest_for(rel)
        if dst is None:
            warn(f"未知 payload 路径，跳过：{rel}")
            continue
        same = dst.is_file() and dst.stat().st_size == src.stat().st_size and sha256(dst) == meta["sha256"]
        if same and not force:
            continue
        changed += 1
        if check:
            log(f"需要更新：{dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"已更新：{dst}")
    return changed


def set_user_env(name: str, value: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            current, _ = winreg.QueryValueEx(key, name)
    except OSError:
        current = None
    if current == value:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    log(f"已设置用户环境变量 {name}={value}（新进程生效）")
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
        )
    except Exception:  # noqa: BLE001
        pass


def read_user_env(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def check_import() -> int:
    env = dict(os.environ)
    env["RENDERDOC_PYTHON_PATH"] = str(module_dir())
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import renderdoc; print('renderdoc', renderdoc.GetVersionString());"
        "import rdc; print('rdc', rdc.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(module_dir())], env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        warn("import 自检失败：\n" + (result.stderr or result.stdout).strip())
        return 1
    for line in result.stdout.strip().splitlines():
        log(line)
    return 0


def verify(manifest: dict, check_env: bool = True) -> int:
    problems = 0
    for rel, meta in manifest.get("files", {}).items():
        dst = dest_for(rel)
        if dst is None:
            continue
        if not dst.is_file():
            warn(f"缺失：{dst}")
            problems += 1
        elif sha256(dst) != meta["sha256"]:
            warn(f"内容不一致：{dst}")
            problems += 1

    if check_env:
        env_value = read_user_env("RENDERDOC_PYTHON_PATH")
        if env_value is None:
            warn("用户环境变量 RENDERDOC_PYTHON_PATH 未设置")
            problems += 1
        elif Path(env_value) != module_dir():
            warn(f"RENDERDOC_PYTHON_PATH={env_value}，期望 {module_dir()}")
            problems += 1

    problems += check_import()

    if not problems:
        log("校验通过：payload、环境变量、Python 导入均就绪")
    return problems


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线安装 rdc-cli + 预编译 RenderDoc 模块（Windows x64 / CPython 3.12）。")
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    parser.add_argument("--force", action="store_true", help="无条件重新复制 payload")
    parser.add_argument("--no-pip", action="store_true", help="只装 payload，跳过 rdc-cli")
    parser.add_argument("--no-env", action="store_true", help="不写用户环境变量（由外部/策略管理时用）")
    parser.add_argument("--python", default=None, help="用指定的 CPython 3.12 解释器执行")
    parser.add_argument("--pip-args", default="", help="透传给 pip 的额外参数（如内网镜像）")
    parser.add_argument("--verify-bundle", action="store_true", help="只校验本包完整性（MANIFEST.sha256），不安装")
    return parser.parse_args(argv)


def verify_bundle() -> int:
    """Check this folder against MANIFEST.sha256 (transit damage check)."""
    path = BUNDLE / "MANIFEST.sha256"
    if not path.is_file():
        die(f"缺少 {path}")
    problems = 0
    count = 0
    for line in path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        expected, _, rel = line.partition("  ")
        target = BUNDLE / rel
        count += 1
        if not target.is_file():
            warn(f"缺失：{rel}")
            problems += 1
        elif sha256(target) != expected:
            warn(f"内容不一致：{rel}")
            problems += 1
    if problems:
        warn(f"{problems}/{count} 个文件有问题，重新拷贝整个包")
        return 1
    log(f"包完整性校验通过：{count} 个文件")
    return 0


def rdc_script() -> Path:
    """Locate the rdc.exe console script for the interpreter running this script."""
    candidates = [
        Path(sys.executable).parent / "rdc.exe",
        Path(sys.executable).parent / "Scripts" / "rdc.exe",
        Path(sys.prefix) / "Scripts" / "rdc.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("rdc")
    return Path(found) if found else candidates[0]


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.verify_bundle:
        return verify_bundle()

    maybe_reexec(args)
    check_python()

    manifest = load_manifest()
    verify_source(manifest)
    log(f"bundle：{BUNDLE}")
    log(f"payload：RenderDoc {manifest['renderdoc']['version']} / {manifest['renderdoc']['python']}"
        f" / commit {manifest['renderdoc']['commit'][:8]}")

    if args.check:
        problems = verify(manifest, check_env=not args.no_env)
        if not args.no_pip:
            log("检查 wheels 是否可离线解析")
            problems += 1 if run_pip(check=True, extra=args.pip_args) != 0 else 0
        return 1 if problems else 0

    changed = install_payload(manifest, args.force, check=False)
    log(f"模块与 APK：{changed} 个文件更新")

    if not args.no_pip:
        log(f"pip install --no-index --find-links wheels -r requirements.txt（Python {sys.version_info.major}.{sys.version_info.minor}）")
        if run_pip(check=False, extra=args.pip_args) != 0:
            die("rdc-cli 依赖安装失败；见 INSTALL.md 排错（可加 --pip-args \"--user\"）")

    if not args.no_env:
        set_user_env("RENDERDOC_PYTHON_PATH", str(module_dir()))
    else:
        log("跳过环境变量写入（--no-env）；请自行确保 RENDERDOC_PYTHON_PATH 指向 "
            f"{module_dir()}")

    log("完成。下一步：")
    log(f"  1) {rdc_script()} --version   （或新开终端后 rdc --version）")
    log("  2) rdc doctor          # 期望 renderdoc-module / renderdoccmd / android-apk 全 [ok]")
    log("  3) rdc install-skill   # 安装 rdc-cli skill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
