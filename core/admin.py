"""Admin-elevation helper for the frozen Windows build (step 13).

WinDivert (used by the inject path) needs Administrator rights. The packaged
single-file exe must therefore re-launch itself *elevated* the first time it's
started without admin. We isolate that logic here so:

* the GUI entry point (`app.py`) stays a thin caller, and
* the *decision* logic (do we need to elevate? build the relaunch argv) is pure
  and unit-testable on any OS — only the actual ``ShellExecuteW`` call is
  Windows-only and is skipped/guarded elsewhere.

On non-Windows or when already elevated, :func:`ensure_admin` is a no-op and
returns ``False`` (meaning "no relaunch happened, keep running").
"""
from __future__ import annotations

import sys
from typing import List, Optional


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_frozen() -> bool:
    """True when running inside a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def is_admin(checker=None) -> bool:
    """Return True if the current process has Administrator rights.

    *checker* lets tests inject a fake; in production it defaults to the
    Win32 ``IsUserAnAdmin`` call. On non-Windows we report True (nothing to
    elevate to), so callers treat it as "already sufficient".
    """
    if checker is not None:
        return bool(checker())
    if not is_windows():
        return True
    try:  # pragma: no cover - Windows-only
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover
        return False


def relaunch_params(argv: Optional[List[str]] = None,
                    frozen: Optional[bool] = None) -> tuple[str, str]:
    """Compute ``(executable, parameters)`` for an elevated relaunch.

    * Frozen build → the exe relaunches itself with the *same* CLI args (the
      script path is already baked in), so parameters are just ``argv[1:]``.
    * Dev / interpreter → relaunch the Python interpreter with the script path
      followed by the original args (``argv``).

    Pure string assembly — no side effects — so it is fully unit-tested.
    """
    if argv is None:
        argv = sys.argv
    if frozen is None:
        frozen = is_frozen()
    if frozen:
        exe = sys.executable
        params = subprocess_list2cmdline(argv[1:])
    else:
        exe = sys.executable
        params = subprocess_list2cmdline(argv)
    return exe, params


def subprocess_list2cmdline(args: List[str]) -> str:
    """Quote an argv list into a Windows command line (stdlib, cross-platform).

    Uses :func:`subprocess.list2cmdline`, which implements the MS C runtime
    quoting rules — the same rules ``ShellExecuteW`` parameters expect.
    """
    import subprocess
    return subprocess.list2cmdline(list(args))


def ensure_admin(argv: Optional[List[str]] = None, *,
                 is_admin_checker=None, runner=None) -> bool:
    """Relaunch elevated if needed; return True iff a relaunch was triggered.

    The caller should exit when this returns True (a new, elevated process has
    been spawned). On non-Windows, already-admin, or when elevation isn't
    applicable, it returns False and the process keeps running.

    *runner* lets tests capture the ShellExecute call without touching Win32.
    """
    if not is_windows():
        return False
    if is_admin(is_admin_checker):
        return False
    exe, params = relaunch_params(argv)
    if runner is not None:
        runner(exe, params)
        return True
    try:  # pragma: no cover - Windows-only
        import ctypes
        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return True
    except Exception:  # pragma: no cover
        # If elevation fails we fall through and let the app try anyway.
        return False
