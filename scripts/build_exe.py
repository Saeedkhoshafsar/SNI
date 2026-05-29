"""One-shot Windows build helper for SNI Spoofer (step 13).

Usage (on Windows, in an activated venv)::

    pip install -r requirements.txt -r requirements-build.txt
    python scripts/build_exe.py

Steps performed:

1. Sanity checks — must run on Windows, PyInstaller must be importable, the
   ``bin/`` binaries must be present.
2. (Re)generate the icon if Pillow is available and the .ico is missing.
3. Clean previous ``build/`` and ``dist/`` outputs.
4. Invoke PyInstaller against ``SNISpoofer.spec``.
5. Report the resulting ``dist/SNISpoofer.exe``.

The script is intentionally import-safe on any OS (it only *runs* the build on
Windows) so CI on Linux can at least import it and the unit tests can reference
its helpers.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "SNISpoofer.spec")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
ICON = os.path.join(ROOT, "assets", "app.ico")
BIN = os.path.join(ROOT, "bin")


def _have_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except Exception:
        return False


def preflight() -> list[str]:
    """Return a list of problems; empty list means good to go."""
    problems = []
    if not sys.platform.startswith("win"):
        problems.append(
            "build target is Windows; run this on Windows (current: "
            f"{sys.platform}).")
    if not _have_pyinstaller():
        problems.append(
            "PyInstaller not installed — `pip install -r requirements-build.txt`.")
    if not os.path.isdir(BIN) or not os.listdir(BIN):
        problems.append(f"bin/ is empty or missing at {BIN}.")
    for need in ("xray.exe", "wintun.dll"):
        if not os.path.isfile(os.path.join(BIN, need)):
            problems.append(f"missing bundled binary: bin/{need}")
    return problems


def ensure_icon() -> None:
    if os.path.isfile(ICON):
        return
    try:
        # Lazy import so the absence of Pillow doesn't break the module import.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import make_icon  # type: ignore
        make_icon.main()
    except Exception as exc:  # pragma: no cover
        print(f"[warn] could not generate icon: {exc}")


def clean() -> None:
    for d in (BUILD, DIST):
        if os.path.isdir(d):
            print(f"[clean] removing {d}")
            shutil.rmtree(d, ignore_errors=True)


def build() -> int:
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC]
    print("[build] " + " ".join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def main() -> int:
    problems = preflight()
    if problems:
        print("Cannot build — resolve the following first:")
        for p in problems:
            print("  - " + p)
        return 1
    ensure_icon()
    clean()
    rc = build()
    if rc != 0:
        print(f"[error] PyInstaller exited with {rc}")
        return rc
    exe = os.path.join(DIST, "SNISpoofer.exe")
    if os.path.isfile(exe):
        size_mb = os.path.getsize(exe) / (1024 * 1024)
        print(f"[ok] built {exe} ({size_mb:.1f} MB)")
        return 0
    print("[error] build finished but dist/SNISpoofer.exe not found")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
