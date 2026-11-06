#!/usr/bin/env python3
"""Assemble the offline (intranet) bundle for a built RenderDoc payload.

CI calls this after ``scripts/ci_build_payload.py`` has produced the module and
the Android APKs have been downloaded. Everything is hashed and laid out the way
``agents/rdc_install.py`` (dotfiles) expects:

    rdc-cli-<renderdoc_version>/
      INSTALL.md              rendered from scripts/offline/INSTALL.md
      install_offline.py      offline installer (no venv/uv/compiler needed)
      requirements.txt        generated from the wheel filenames
      MANIFEST.sha256         sha256 of every file in the bundle
      wheels/                 rdc-cli + click + Pillow + numpy (cp3XX / win_amd64)
      payload/win-x64/
        MANIFEST.json         renderdoc version/commit/python + per-file sha256
        renderdoc/            renderdoc.pyd, renderdoc.dll, renderdoccmd.exe,
                              VC runtime DLLs, LICENSE.md
        android/              org.renderdoc.renderdoccmd.arm{32,64}.apk

Usage:
    python scripts/build_offline_bundle.py \
        --renderdoc-version 1.47 \
        --renderdoc-source .build/renderdoc/renderdoc \
        --payload-dir "%LOCALAPPDATA%/rdc/renderdoc" \
        --apk-dir "%LOCALAPPDATA%/rdc/share/renderdoc/plugins/android" \
        --wheels-dir dist-wheels \
        --out dist
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "scripts" / "offline"
MODULE_FILES = ("renderdoc.pyd", "renderdoc.dll", "renderdoccmd.exe")
VC_RUNTIME_FILES = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def _configure_console() -> None:
    """Never crash on a cp1252/cp936 console (GitHub runner and legacy cmd)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a TextIOWrapper
            pass


_configure_console()


def log(message: str) -> None:
    print(f"[bundle] {message}", flush=True)


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"[bundle] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def android_version_code(version: str) -> int:
    """RenderDoc's APK versionCode is major*100+minor (1.47 -> 147)."""
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        die(f"无法解析 RenderDoc 版本号：{version}")
    return int(match.group(1)) * 100 + int(match.group(2))


def git_commit(src: Path) -> str:
    result = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True)
    if result.returncode != 0:
        die(f"无法读取 RenderDoc commit（{src}）：{result.stderr.strip()}")
    return result.stdout.strip()


def wheel_requirements(wheels: list[Path]) -> list[str]:
    """Turn wheel filenames into pinned requirement lines."""
    lines = []
    for wheel in wheels:
        stem = wheel.name[: -len(".whl")]
        parts = stem.split("-")
        if len(parts) < 3:
            die(f"无法解析 wheel 文件名：{wheel.name}")
        name = parts[0].replace("_", "-")
        lines.append(f"{name}=={parts[1]}")
    return sorted(lines, key=str.lower)


def collect_wheels(wheels_dir: Path) -> list[Path]:
    if not wheels_dir.is_dir():
        die(f"wheels 目录不存在：{wheels_dir}")
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        die(f"wheels 目录为空：{wheels_dir}")
    if not any(w.name.startswith(("rdc_cli-", "rdc-cli-")) for w in wheels):
        die("wheels 里缺少 rdc-cli 自己的 wheel（先 `pip wheel . --no-deps -w <dir>`）")
    return wheels


def copy_module(payload_dir: Path, dest: Path, vc_runtime_dir: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in MODULE_FILES:
        src = payload_dir / name
        if not src.is_file():
            die(f"缺少构建产物：{src}")
        shutil.copy2(src, dest / name)

    for name in VC_RUNTIME_FILES:
        src = vc_runtime_dir / name
        if not src.is_file():
            die(f"缺少 VC 运行库：{src}（用 --vc-runtime-dir 指向包含这些 DLL 的目录）")
        shutil.copy2(src, dest / name)


def copy_apks(apk_dir: Path, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    apks = sorted(apk_dir.glob("*.apk"))
    if len(apks) < 2:
        die(f"Android APK 不足（{len(apks)} 个）：{apk_dir}（先 `rdc setup-renderdoc --android`）")
    for apk in apks:
        shutil.copy2(apk, dest / apk.name)
    return apks


def write_payload_manifest(root: Path, version: str, commit: str, python: str, apk_version: int) -> None:
    files = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        files[path.relative_to(root).as_posix()] = {"sha256": sha256(path), "size": path.stat().st_size}
    manifest = {
        "platform": "win-x64",
        "renderdoc": {"version": version, "commit": commit, "python": python},
        "android": {"versionCode": apk_version, "versionName": commit},
        "generated": __import__("datetime").date.today().isoformat(),
        "files": files,
    }
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def render_install(version: str, commit: str, rdc_version: str, python: str) -> str:
    template = (TEMPLATES / "INSTALL.md").read_text(encoding="utf-8")
    return (
        template.replace("{{RDOC_VERSION}}", version)
        .replace("{{RDOC_COMMIT}}", commit[:8])
        .replace("{{RDC_VERSION}}", rdc_version)
        .replace("{{RDC_PYTHON}}", python)
    )


def write_bundle_manifest(root: Path) -> int:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            lines.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    (root / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")
    return len(lines)


def make_zip(root: Path) -> Path:
    archive_path = root.parent / f"{root.name}.zip"
    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(root.rglob("*")):
            arcname = (Path(root.name) / path.relative_to(root)).as_posix()
            if path.is_dir():
                archive.writestr(arcname + "/", "")
            else:
                archive.write(path, arcname)
    return archive_path


def release_notes(name: str, archive: Path, version: str, commit: str, rdc_version: str) -> str:
    return (
        f"# {name}\n\n"
        "离线（内网）安装包：Windows x64 + CPython 3.12，**不需要编译 RenderDoc、不需要 venv、不需要联网**。\n\n"
        "| 组件 | 版本 |\n|---|---|\n"
        f"| RenderDoc 模块 | {version} (`{commit[:8]}`) |\n"
        f"| rdc-cli | {rdc_version} |\n"
        "| 目标平台 | win-x64 / cp312 |\n\n"
        "## 安装\n\n"
        "```powershell\n"
        f"Expand-Archive {archive.name} .\n"
        f"cd {name}\n"
        "python install_offline.py --verify-bundle\n"
        "python install_offline.py\n"
        "rdc doctor\n"
        "rdc install-skill\n"
        "```\n\n"
        f"`{archive.name}.sha256` 为校验文件；细节见包内 `INSTALL.md`。\n"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="构建 rdc-cli 离线安装包（Windows x64）。")
    parser.add_argument("--renderdoc-version", required=True, help="RenderDoc 版本，如 1.47")
    parser.add_argument("--renderdoc-source", required=True, help="RenderDoc 源码目录（取 commit 与 LICENSE.md）")
    parser.add_argument("--payload-dir", required=True, help="构建产物目录（renderdoc.pyd 所在）")
    parser.add_argument("--apk-dir", required=True, help="Android APK 目录")
    parser.add_argument("--wheels-dir", required=True, help="预下载好的 wheels 目录")
    parser.add_argument("--vc-runtime-dir", default=os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"))
    parser.add_argument("--rdc-version", default=None, help="rdc-cli 版本（默认从 wheel 推断）")
    parser.add_argument("--out", default=str(REPO / "dist"), help="输出目录")
    parser.add_argument("--no-zip", action="store_true", help="只生成目录")
    args = parser.parse_args(argv)

    version = args.renderdoc_version.lstrip("v")
    src = Path(args.renderdoc_source).resolve()
    commit = git_commit(src)
    python = python_tag()
    if python != "cp312":
        log(f"警告：当前解释器是 {python}；bundle 的 INSTALL.md 按 3.12 编写，"
            "若用别的 ABI 请同步 scripts/offline/INSTALL.md")

    wheels = collect_wheels(Path(args.wheels_dir).resolve())
    rdc_version = args.rdc_version or wheel_requirements(wheels)[0].split("==")[1]
    for line in wheel_requirements(wheels):
        if line.lower().startswith("rdc-cli=="):
            rdc_version = line.split("==")[1]

    name = f"rdc-cli-{version}"
    out = Path(args.out).resolve() / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    payload = out / "payload" / "win-x64"
    copy_module(Path(args.payload_dir).resolve(), payload / "renderdoc", Path(args.vc_runtime_dir).resolve())
    license_src = src / "LICENSE.md"
    if not license_src.is_file():
        die(f"缺少 RenderDoc LICENSE.md：{license_src}")
    shutil.copy2(license_src, payload / "renderdoc" / "LICENSE.md")

    apks = copy_apks(Path(args.apk_dir).resolve(), payload / "android")
    log(f"APK：{', '.join(a.name for a in apks)}")

    write_payload_manifest(payload, version, commit, python, android_version_code(version))

    wheels_out = out / "wheels"
    wheels_out.mkdir()
    for wheel in wheels:
        shutil.copy2(wheel, wheels_out / wheel.name)
    (out / "requirements.txt").write_text("\n".join(wheel_requirements(wheels)) + "\n", encoding="utf-8")

    (out / "INSTALL.md").write_text(render_install(version, commit, rdc_version, python), encoding="utf-8")
    shutil.copy2(TEMPLATES / "install_offline.py", out / "install_offline.py")

    count = write_bundle_manifest(out)
    log(f"{name}: {count} 个文件（RenderDoc {version} / rdc-cli {rdc_version} / {python}）")

    notes = release_notes(name, out.parent / f"{name}.zip", version, commit, rdc_version)
    (out.parent / f"{name}-notes.md").write_text(notes, encoding="utf-8")

    if args.no_zip:
        log(f"完成：{out}")
        return 0

    archive = make_zip(out)
    digest = sha256(archive)
    (out.parent / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    log(f"完成：{archive}（{archive.stat().st_size / 1024 / 1024:.1f} MB, sha256 {digest[:16]}…）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
