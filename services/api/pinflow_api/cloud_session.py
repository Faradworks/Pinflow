"""Process-held Pinflow Cloud session, with silent token refresh.

The local service runs on one machine, so "who is signed in" is a single
process-global. On login (routes/auth.py callback) the captured Clerk JWT is
**exchanged** at the gateway for a short-lived access token + a long-lived
refresh token; `get_token()` then auto-refreshes the access token via the
gateway — no browser, no Clerk round-trip — until the refresh token expires.

If the gateway is unreachable at login (or no `PINFLOW_CLOUD_URL`), we fall back
to holding the raw Clerk JWT directly (no refresh). The token never reaches the
renderer. The session is **persisted to a user-only file**
(`~/.pinflow/cloud_session.json`, mode 0600) so it survives app restarts — the
refresh token keeps it alive until it expires, instead of every relaunch
signing the user out.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from pinflow_api.settings import settings

_PENDING_TTL_S = 600       # forget unfinished login attempts after 10 min
_REFRESH_SLACK_S = 60      # refresh the access token this long before it expires


@dataclass
class CloudSession:
    access_token: str
    refresh_token: Optional[str] = None
    access_exp: float = 0.0
    user_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    captured_at: float = 0.0


_lock = threading.Lock()
_session: Optional[CloudSession] = None
_pending: dict[str, dict] = {}  # state nonce -> {"created": ts, "done": bool}


# ---------------------------------------------------------------------------
# Disk persistence — survives app restarts (holds a long-lived refresh token,
# so the file is user-only + written atomically). Best-effort: a disk error
# never breaks sign-in/out.


def _session_path() -> Path:
    base = os.environ.get("PINFLOW_STATE_DIR") or os.path.expanduser("~/.pinflow")
    return Path(base) / "cloud_session.json"


def _persist(sess: Optional[CloudSession]) -> None:
    path = _session_path()
    try:
        if sess is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "access_token": sess.access_token,
            "refresh_token": sess.refresh_token,
            "access_exp": sess.access_exp,
            "user_id": sess.user_id,
            "email": sess.email,
            "name": sess.name,
            "captured_at": sess.captured_at,
        }))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        pass


def _load() -> None:
    """Restore a persisted session on startup. A stale access token is fine —
    get_token refreshes it via the still-valid refresh token on first use."""
    global _session
    try:
        path = _session_path()
        if not path.exists():
            return
        d = json.loads(path.read_text())
        if not (d.get("refresh_token") or d.get("access_token")):
            return
        _session = CloudSession(
            access_token=d.get("access_token") or "",
            refresh_token=d.get("refresh_token"),
            access_exp=float(d.get("access_exp") or 0),
            user_id=d.get("user_id"),
            email=d.get("email"),
            name=d.get("name"),
            captured_at=float(d.get("captured_at") or 0),
        )
    except Exception:
        _session = None


def decode_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload (no signature check — display only)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _gateway() -> Optional[str]:
    return (settings.pinflow_cloud_url or "").strip().rstrip("/") or None


def _exchange(gw: str, clerk_token: str) -> Optional[dict]:
    try:
        r = httpx.post(f"{gw}/v1/auth/exchange", json={"token": clerk_token}, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _refresh(gw: str, refresh_token: str) -> Optional[dict]:
    try:
        r = httpx.post(f"{gw}/v1/auth/refresh", json={"refresh_token": refresh_token}, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def start_login(state: str) -> None:
    with _lock:
        now = time.time()
        _pending[state] = {"created": now, "done": False}
        for s in [k for k, v in _pending.items() if now - v["created"] > _PENDING_TTL_S]:
            _pending.pop(s, None)


def complete_login(token: str, *, state: Optional[str] = None) -> CloudSession:
    """Capture a sign-in. Exchanges the Clerk JWT for gateway access + refresh
    tokens when possible (enables silent refresh); else holds the raw token."""
    claims = decode_claims(token)
    user_id = claims.get("sub") or token
    email = claims.get("email")
    name = claims.get("name") or claims.get("first_name")

    access, refresh = token, None
    access_exp = float(claims.get("exp", 0)) or (time.time() + 60)

    gw = _gateway()
    if gw:
        ex = _exchange(gw, token)
        if ex and ex.get("access_token"):
            access = ex["access_token"]
            refresh = ex.get("refresh_token")
            access_exp = time.time() + int(ex.get("expires_in", 3600))
            user_id = ex.get("user_id") or user_id

    sess = CloudSession(
        access_token=access, refresh_token=refresh, access_exp=access_exp,
        user_id=user_id, email=email, name=name, captured_at=time.time(),
    )
    with _lock:
        global _session
        _session = sess
        if state and state in _pending:
            _pending[state]["done"] = True
    _persist(sess)
    return sess


def get_token() -> Optional[str]:
    """The current access token, refreshed in place if it's near expiry."""
    with _lock:
        s = _session
        if s is None:
            return None
        access, exp, refresh = s.access_token, s.access_exp, s.refresh_token

    if refresh and time.time() >= exp - _REFRESH_SLACK_S:
        gw = _gateway()
        new = _refresh(gw, refresh) if gw else None
        if new and new.get("access_token"):
            with _lock:
                if _session is not None:
                    _session.access_token = new["access_token"]
                    _session.access_exp = time.time() + int(new.get("expires_in", 3600))
                    refreshed = _session
                    access = _session.access_token
            _persist(refreshed)
            return access
        # refresh failed → return the (stale) token; the gateway 401 will surface
        # "session expired" and the user re-signs in.
    return access


def logout() -> None:
    with _lock:
        global _session
        _session = None
    _persist(None)


def status(state: Optional[str] = None) -> dict:
    with _lock:
        out: dict = {
            "signed_in": _session is not None,
            "pending": bool(state and state in _pending and not _pending[state]["done"]),
        }
        if _session:
            out["user_id"] = _session.user_id
            out["email"] = _session.email
            out["name"] = _session.name
        return out


# Restore any persisted session on import so the sidecar stays signed in across
# app restarts (the refresh token then keeps the access token alive).
_load()
