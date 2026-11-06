#!/usr/bin/env python3
"""Build only the RenderDoc artifacts the offline bundle needs (Windows x64).

This is the CI entry point behind ``.github/workflows/offline-bundle.yml``. It
reuses the helpers from :mod:`rdc._build_renderdoc` (vswhere/MSBuild discovery,
``python.props`` patching for the running interpreter, artifact copying) but
builds individual project files instead of the whole solution.

Why not the solution: ``renderdoc.sln`` contains ``qrenderdoc`` (Qt GUI) and the
solution build also relies on solution-level ordering, so it needs a Qt SDK.
Targets that avoid it are not usable here — MSBuild maps ``/t:<Project>`` on this
solution to a *target name inside every project* (MSB4057). Building the project
files directly with an explicit dependency chain avoids Qt entirely:

    breakpad common / crash_generation_{client,server} / exception_handler
        -> pyrenderdoc_module (pulls renderdoc.dll + drivers via ProjectReference)
        -> renderdoccmd

``SolutionDir`` must be passed because RenderDoc's project files resolve imports
and output paths through it.

Usage:
    python scripts/ci_build_payload.py --version v1.47 \
        --out "%LOCALAPPDATA%\\rdc\\renderdoc" --build-dir .build/renderdoc

Exit codes: 0 = artifacts built (or already present), 1 = failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# (relative project path, human label) — order matters: link-time dependencies
# of renderdoc.vcxproj / renderdoccmd.vcxproj are not expressed as ProjectReference.
PROJECTS = (
    ("renderdoc/3rdparty/breakpad/client/windows/common.vcxproj", "breakpad common"),
    (
        "renderdoc/3rdparty/breakpad/client/windows/crash_generation/crash_generation_client.vcxproj",
        "crash_generation_client",
    ),
    (
        "renderdoc/3rdparty/breakpad/client/windows/crash_generation/crash_generation_server.vcxproj",
        "crash_generation_server",
    ),
    (
        "renderdoc/3rdparty/breakpad/client/windows/handler/exception_handler.vcxproj",
        "exception_handler",
    ),
    ("qrenderdoc/Code/pyrenderdoc/pyrenderdoc_module.vcxproj", "pyrenderdoc_module"),
    ("renderdoccmd/renderdoccmd.vcxproj", "renderdoccmd"),
)

MSBUILD_FLAGS = ["/p:Configuration=Release", "/p:Platform=x64", "/p:PlatformToolset=v143"]


def _configure_console() -> None:
    """Never crash on a cp1252/cp936 console (GitHub runner and legacy cmd)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - not a TextIOWrapper
            pass


_configure_console()


def log(message: str) -> None:
    print(f"[payload] {message}", flush=True)


def die(message: str) -> "NoReturn":  # noqa: F821
    print(f"[payload] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def require_windows() -> None:
    if sys.platform != "win32":
        die("this helper only builds the Windows payload (win-x64)")


def import_builder():
    """Import rdc's build helpers, failing loudly if upstream renames them."""
    try:
        from rdc import _build_renderdoc as builder
    except ImportError as exc:  # pragma: no cover - CI guard
        die(f"cannot import rdc._build_renderdoc: {exc}（先 `pip install .`）")

    missing = [
        name
        for name in ("clone_renderdoc", "copy_artifacts", "_prepare_win_python", "_find_msbuild")
        if not hasattr(builder, name)
    ]
    if missing:
        die(f"rdc._build_renderdoc 缺少 {missing}，上游实现已变更，请同步 scripts/ci_build_payload.py")
    return builder


def artifacts_present(out: Path) -> bool:
    return all((out / name).is_file() for name in ("renderdoc.pyd", "renderdoc.dll"))


def run_msbuild(builder, src_dir: Path, jobs: int) -> None:
    msbuild = str(builder._find_msbuild())
    prefix = builder._prepare_win_python(src_dir)

    env = dict(os.environ)
    # RenderDoc <= v1.41 vs >= v1.42 (VSPythonOverridePath) property names
    env["RENDERDOC_PYTHON_PREFIX64"] = str(prefix)
    env["VSPythonOverridePath"] = str(prefix)
    env["CL"] = (env.get("CL", "") + " /wd4996").strip()

    for relative, label in PROJECTS:
        project = src_dir / relative
        if not project.is_file():
            die(f"项目文件不存在：{project}（RenderDoc 版本差异？）")
        cmd = [
            msbuild,
            str(project),
            *MSBUILD_FLAGS,
            f"/p:SolutionDir={src_dir}{os.sep}",
            f"/m:{jobs}",
            "/v:minimal",
            "/nologo",
        ]
        log(f"MSBuild：{label}")
        subprocess.run(cmd, check=True, env=env)


def verify_import(out: Path, version: str) -> None:
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import renderdoc;"
        "v = renderdoc.GetVersionString();"
        "print('renderdoc', v);"
        "assert v.lstrip('v').startswith(sys.argv[2]), f'built {v} != requested {sys.argv[2]}'"
    )
    wanted = version.lstrip("v")
    result = subprocess.run([sys.executable, "-c", code, str(out), wanted], capture_output=True, text=True)
    if result.returncode != 0:
        die("模块自检失败：\n" + (result.stderr or result.stdout).strip())
    for line in result.stdout.strip().splitlines():
        log(line)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build the RenderDoc module used by the offline bundle.")
    parser.add_argument("--version", required=True, help="RenderDoc tag, e.g. v1.47")
    parser.add_argument("--out", default=None, help="Install dir (default: %%LOCALAPPDATA%%\\rdc\\renderdoc)")
    parser.add_argument("--build-dir", default=".build/renderdoc", help="Clone/build cache dir")
    parser.add_argument("--jobs", type=int, default=None, help="MSBuild parallelism")
    parser.add_argument("--force", action="store_true", help="Rebuild even if artifacts exist")
    args = parser.parse_args(argv)

    require_windows()
    builder = import_builder()

    build_dir = Path(args.build_dir).resolve()
    out = Path(args.out).resolve() if args.out else builder.default_install_dir()

    if artifacts_present(out) and not args.force:
        log(f"已存在构建产物：{out}（--force 可强制重建）")
        verify_import(out, args.version)
        return 0

    log(f"RenderDoc {args.version} → {out}")
    builder.clone_renderdoc(build_dir, args.version)
    src_dir = build_dir / "renderdoc"
    run_msbuild(builder, src_dir, args.jobs or os.cpu_count() or 4)
    builder.copy_artifacts(build_dir, out, "windows")
    verify_import(out, args.version)
    log("构建完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
