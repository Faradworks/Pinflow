"""Sidecar / frozen-app entry point.

Runs the FastAPI service with uvicorn against the imported ``app`` object
(no reload, no import-string) so it works inside a PyInstaller bundle — the
Tauri desktop spawns this as an ``externalBin`` sidecar. Dev still uses
``uvicorn pinflow_api.main:app --reload`` via scripts/dev.sh.

Host/port are env-overridable so the Rust shell can hand it a free port.
"""
from __future__ import annotations

import os
import threading
import time

import uvicorn

from pinflow_api.main import app


def _exit_when_parent_gone() -> None:
    """Self-terminate when the desktop shell that launched us dies (quit or
    crash) so we never orphan and keep holding the port. The shell passes its
    own PID as ``PINFLOW_PARENT_PID``; we watch that directly because
    PyInstaller's onefile bootloader hides the real parent (our ``os.getppid()``
    is the bootloader, not the shell)."""
    raw = os.environ.get("PINFLOW_PARENT_PID")
    if not raw:
        return
    parent = int(raw)
    while True:
        try:
            os.kill(parent, 0)  # signal 0 → existence check only
        except OSError:
            os._exit(0)
        time.sleep(1.0)


def main() -> None:
    host = os.environ.get("PINFLOW_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PINFLOW_API_PORT", "8787"))
    threading.Thread(target=_exit_when_parent_gone, daemon=True).start()
    uvicorn.run(app, host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
