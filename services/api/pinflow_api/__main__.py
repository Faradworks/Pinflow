"""Sidecar / frozen-app entry point.

Runs the FastAPI service with uvicorn against the imported ``app`` object
(no reload, no import-string) so it works inside a PyInstaller bundle — the
Tauri desktop spawns this as an ``externalBin`` sidecar. Dev still uses
``uvicorn pinflow_api.main:app --reload`` via scripts/dev.sh.

Host/port are env-overridable so the Rust shell can hand it a free port.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import uvicorn

from pinflow_api.main import app


if sys.platform == "win32":
    import ctypes

    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102

    def _parent_alive(pid: int) -> bool:
        # On Windows ``os.kill(pid, 0)`` maps to ``TerminateProcess`` and would
        # KILL the parent, not probe it. Open a SYNCHRONIZE handle instead and
        # ask whether the process object is signalled (i.e. has exited).
        handle = ctypes.windll.kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return False  # gone (or unopenable) → treat as dead
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
else:

    def _parent_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)  # signal 0 → existence check only
            return True
        except OSError:
            return False


def _exit_when_parent_gone() -> None:
    """Self-terminate when the desktop shell that launched us dies (quit or
    crash) so we never orphan and keep holding the port. The shell passes its
    own PID as ``PINFLOW_PARENT_PID``; we watch that directly because
    PyInstaller's bootloader hides the real parent (our ``os.getppid()`` is the
    bootloader, not the shell)."""
    raw = os.environ.get("PINFLOW_PARENT_PID")
    if not raw:
        return
    parent = int(raw)
    while True:
        if not _parent_alive(parent):
            os._exit(0)
        time.sleep(1.0)


def main() -> None:
    host = os.environ.get("PINFLOW_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PINFLOW_API_PORT", "8787"))
    threading.Thread(target=_exit_when_parent_gone, daemon=True).start()
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
