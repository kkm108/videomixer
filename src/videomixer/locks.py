"""Cross-platform advisory locks via atomic mkdir with stale takeover."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockTimeout(RuntimeError):
    pass


@contextmanager
def workspace_lock(lock_path: Path,
                   timeout_s: float = 10.0,
                   stale_s: float = 30.0):
    """Serialize access to a shared workspace file.

    Uses atomic directory creation (works identically on Windows/POSIX).
    A lock older than `stale_s` seconds is considered abandoned and stolen,
    so a crashed process can never wedge the workspace permanently.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    acquired = False
    while not acquired:
        try:
            os.mkdir(lock_path)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_s:
                try:
                    os.rmdir(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    f"could not acquire {lock_path} within {timeout_s}s")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.rmdir(lock_path)
        except OSError:
            pass


def touch_lock(lock_path: Path) -> None:
    try:
        os.utime(lock_path, None)
    except OSError:
        pass
