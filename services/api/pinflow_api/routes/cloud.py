"""Local proxy to the Pinflow Cloud gateway for credits + top-up.

The desktop never holds the cloud session token — this local service does
(cloud_session). These endpoints forward to the gateway (PINFLOW_CLOUD_URL) with
that token so the desktop can show a balance and start a top-up. Everything
degrades gracefully when not signed in or when the gateway URL is unset.
"""

from __future__ import annotations

import webbrowser

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from pinflow_api import cloud_session
from pinflow_api.settings import settings

router = APIRouter(prefix="/cloud")


def _gateway() -> str | None:
    return settings.pinflow_cloud_url.rstrip("/") if settings.pinflow_cloud_url else None


def _auth() -> dict | None:
    tok = cloud_session.get_token()
    return {"x-api-key": tok} if tok else None


@router.get("/credits")
async def credits():
    gw, h = _gateway(), _auth()
    if not gw:
        return {"signed_in": bool(h), "configured": False}
    if not h:
        return {"signed_in": False, "configured": True}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{gw}/v1/credits", headers=h)
        if r.status_code == 401:
            # The token (and its refresh token) is dead — a stale in-memory
            # session. Clear it so the chip flips to signed-out and prompts a
            # re-auth, instead of reporting signed_in:true off a zombie session.
            # This probe runs on mount/focus, so expiry surfaces here before the
            # user spends a prompt and hits the same 401 in the agent loop.
            cloud_session.logout()
            return {"signed_in": False, "configured": True}
        if r.status_code != 200:
            return {"signed_in": True, "configured": True, "error": f"gateway {r.status_code}"}
        d = r.json()
        return {
            "signed_in": True,
            "configured": True,
            "balance": d.get("balance"),
            "next_expiry": d.get("next_expiry"),
        }
    except Exception as e:
        return {"signed_in": True, "configured": True, "error": str(e)}


@router.post("/topup")
async def topup(request: Request):
    gw, h = _gateway(), _auth()
    if not gw:
        return {"ok": False, "reason": "cloud_not_configured"}
    if not h:
        return {"ok": False, "reason": "not_signed_in"}
    body = await request.json()
    try:
        amount = int(body.get("amount_usd", 0))
    except (TypeError, ValueError):
        amount = 0
    base = str(request.base_url).rstrip("/")
    success_url = f"{base}/cloud/topup/callback?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/cloud/topup/cancel"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{gw}/v1/billing/topup",
                headers=h,
                json={"amount_usd": amount, "success_url": success_url, "cancel_url": cancel_url},
            )
        if r.status_code != 200:
            return {"ok": False, "reason": f"gateway {r.status_code}", "detail": r.text[:300]}
        url = r.json().get("url")
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    opened = False
    if url:
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
    return {"ok": True, "checkout_url": url, "opened": opened}


@router.get("/topup/callback")
async def topup_callback(session_id: str = ""):
    # The success redirect lands here. Drive the grant via reconcile and report the
    # ACTUAL outcome — don't claim "credits added" unconditionally. (A swallowed
    # reconcile failure here once masked a gateway bug where every grant silently
    # failed while this page still said success.)
    outcome = await _reconcile(session_id)
    if outcome == "added":
        return HTMLResponse(_close_page("Payment complete — credits added. You can close this tab."))
    if outcome == "pending":
        return HTMLResponse(_close_page(
            "Payment received — your credits are being finalized and will appear in a moment."
        ))
    return HTMLResponse(_close_page(
        "Payment received, but we couldn't confirm the credit automatically. It will be "
        "reconciled shortly — if your balance doesn't update, reopen the top-up.",
        auto_close=False,
    ))


async def _reconcile(session_id: str) -> str:
    """POST the Checkout session to the gateway's reconcile and classify the result:
    "added" (granted now, or already on the account), "pending" (paid but not yet
    confirmed), or "error" (not signed in / transport / gateway failure). Never
    raises — this backs a user-facing page."""
    gw, h = _gateway(), _auth()
    if not (gw and h and session_id):
        return "error"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{gw}/v1/billing/reconcile", headers=h, json={"session_id": session_id})
        if r.status_code != 200:
            return "error"
        d = r.json()
        if not d.get("ok"):
            return "error"
        if d.get("granted") or d.get("payment_status") == "paid":
            return "added"
        return "pending"
    except Exception:
        return "error"


@router.get("/topup/cancel")
async def topup_cancel():
    return HTMLResponse(_close_page("Payment canceled. You can close this tab."))


def _close_page(msg: str, *, auto_close: bool = True) -> str:
    # Auto-close on success/cancel; keep the tab open on an error so the user can
    # actually read the message.
    script = (
        "<script>setTimeout(()=>{try{window.close()}catch(e){}},1400)</script>"
        if auto_close else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Pinflow</title>'
        "<style>body{font:14px -apple-system,sans-serif;display:grid;place-items:center;"
        "height:100vh;margin:0;background:#0e0e10;color:#f3f3f1}"
        ".card{padding:28px 32px;border:1px solid #26262a;border-radius:14px;background:#161618}"
        "</style></head><body><div class=\"card\">" + msg + "</div>"
        + script + "</body></html>"
    )
