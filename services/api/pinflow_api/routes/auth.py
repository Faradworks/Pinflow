"""Pinflow Cloud login — loopback, browser-mediated, no Tauri/Rust.

Flow:
1. Desktop `POST /auth/start`. We mint a `state` nonce, build the login URL
   (the hosted Clerk helper page, or our built-in dev page), open the system
   browser to it, and return `{state, login_url}`.
2. The user signs in. The helper page redirects the *browser* back to
   `GET /auth/callback?token=<jwt>&state=<nonce>`. We stash the token in
   `cloud_session` and render a "you can close this tab" page.
3. The desktop polls `GET /auth/status?state=<nonce>` until `signed_in`.

`llm.make_client(cloud)` then reads the stored token — it never goes to the
renderer. `/auth/start` resolves the login page in priority order:
`PINFLOW_LOGIN_URL` (a separately-hosted page) → the built-in `/auth/clerk`
page when `PINFLOW_CLERK_PUBLISHABLE_KEY` is set (same-origin, nothing to host
— the dev / staging-Clerk path) → the `/auth/dev` stub (gateway no-Clerk mode).
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
import webbrowser

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pinflow_api import cloud_session
from pinflow_api.settings import settings

router = APIRouter(prefix="/auth")

# Mounted at the app root (no prefix) in main.py. Social/OAuth sign-in makes Clerk
# bounce its handshake back to the app *origin* ("/"), not to /auth/clerk — so we
# forward it (params intact) to /auth/clerk, which loads Clerk.js and completes
# the handshake. redirect_uri/state are recovered there from sessionStorage.
root_router = APIRouter()


_ROOT_PAGE = (
    '<!doctype html><meta charset="utf-8"><title>Pinflow</title>'
    "<style>body{font:14px -apple-system,sans-serif;display:grid;place-items:center;"
    "min-height:100vh;margin:0;background:#0e0e10;color:#82827a}</style>"
    "<div>Pinflow local service — you can close this tab.</div>"
)


@root_router.get("/")
async def root(request: Request):
    qs = request.url.query
    if "__clerk_handshake" in qs or "__clerk_db_jwt" in qs:
        return RedirectResponse(url=f"/auth/clerk?{qs}", status_code=307)
    # A browser landing here (e.g. a stray post-auth redirect) gets a tidy page
    # instead of bare JSON; programmatic callers still get the JSON.
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(_ROOT_PAGE)
    return {"service": "pinflow-api"}


_CALLBACK_PATH = "/auth/callback"


def _local_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.post("/start")
async def start(request: Request):
    state = secrets.token_urlsafe(16)
    cloud_session.start_login(state)
    base = _local_base(request)
    redirect_uri = base + _CALLBACK_PATH
    query = urllib.parse.urlencode({"redirect_uri": redirect_uri, "state": state})
    if settings.pinflow_login_url:
        target = settings.pinflow_login_url
    elif settings.pinflow_clerk_publishable_key:
        target = f"{base}/auth/clerk"
    else:
        target = f"{base}/auth/dev"
    login_url = f"{target}?{query}"
    try:
        opened = webbrowser.open(login_url)
    except Exception:
        opened = False
    return {"state": state, "login_url": login_url, "opened": opened}


@router.get("/callback")
async def callback(token: str = "", state: str = ""):
    if not token:
        return HTMLResponse(_PAGE.format(msg="Sign-in failed — no token received."), status_code=400)
    cloud_session.complete_login(token, state=state or None)
    return HTMLResponse(_PAGE.format(msg="Signed in to Pinflow. You can close this tab."))


@router.get("/status")
async def status(state: str = ""):
    return cloud_session.status(state or None)


@router.post("/logout")
async def logout():
    cloud_session.logout()
    return {"ok": True}


@router.post("/signout-start")
async def signout_start(request: Request):
    """Full sign-out for account-switching. Clears the local Pinflow session and
    opens the browser to /auth/signout, which also clears the *Clerk* browser
    session — so the next sign-in can choose a different account. (Plain /logout
    clears only Pinflow's side; the Clerk session persists and silently reuses the
    same user.) No-Clerk dev mode just clears locally."""
    cloud_session.logout()
    opened, url = False, None
    if settings.pinflow_clerk_publishable_key.strip():
        url = _local_base(request) + "/auth/signout"
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
    return {"opened": opened, "signout_url": url}


@router.post("/validate-key")
async def validate_key(request: Request):
    """Cheap BYOK key check via Anthropic's GET /v1/models — a metadata call that
    authenticates the key but generates no tokens (no usage billing). 200 → valid,
    401 → invalid; a network/other error is reported as `unknown` (don't gate on it).

    Note: a 200 confirms the key is authentic, NOT that the org has credit or model
    access — the first real chat is the true end-to-end test.
    """
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key:
        return {"valid": False, "error": "empty"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
    except Exception as e:
        return {"valid": False, "unknown": True, "error": f"network: {e}"}
    if r.status_code == 200:
        return {"valid": True}
    if r.status_code == 401:
        return {"valid": False, "error": "invalid or revoked key"}
    return {"valid": False, "unknown": True, "error": f"unexpected {r.status_code}"}


@router.get("/dev")
async def dev_login(redirect_uri: str = "", state: str = ""):
    """Built-in stub sign-in (used when PINFLOW_LOGIN_URL is unset). Posts a
    chosen username straight to the callback as the token — the gateway's
    no-Clerk dev mode treats the token as the user id."""
    return HTMLResponse(_DEV_PAGE(redirect_uri, state))


@router.get("/clerk")
async def clerk_login(redirect_uri: str = "", state: str = ""):
    """Built-in Clerk sign-in page — used when PINFLOW_CLERK_PUBLISHABLE_KEY is set
    and PINFLOW_LOGIN_URL is not. Renders Clerk's <SignIn>; once signed in it mints
    a default session token and hands it to /auth/callback. Same-origin with the
    callback, so no separately-hosted helper page is needed. The token is a 60s
    session JWT — fine, because the callback exchanges it at the gateway at once."""
    pk = settings.pinflow_clerk_publishable_key.strip()
    if not pk:
        return HTMLResponse(
            _PAGE.format(msg="No Clerk publishable key configured."), status_code=400
        )
    return HTMLResponse(_CLERK_PAGE(redirect_uri, state, pk))


@router.get("/signout")
async def signout_page():
    """Clear BOTH the Pinflow session (POST /auth/logout) and the Clerk *browser*
    session (clerk.signOut). The desktop's own sign-out only clears Pinflow's, so
    Clerk's persists and re-signing-in silently reuses the same user. Open this in
    the browser to switch accounts: sign out here, then Sign in again → Clerk
    prompts for an account."""
    pk = settings.pinflow_clerk_publishable_key.strip()
    if not pk:
        return HTMLResponse(
            _PAGE.format(msg="No Clerk publishable key configured."), status_code=400
        )
    return HTMLResponse(_SIGNOUT_PAGE(pk))


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Pinflow</title>
<style>body{{font:14px -apple-system,sans-serif;display:grid;place-items:center;height:100vh;margin:0;background:#0e0e10;color:#f3f3f1}}
.card{{padding:28px 32px;border:1px solid #26262a;border-radius:14px;background:#161618}}</style></head>
<body><div class="card">{msg}</div><script>setTimeout(()=>{{try{{window.close()}}catch(e){{}}}},1200)</script></body></html>"""


def _js(s: str) -> str:
    """A safe JS string literal for embedding in a <script> block."""
    return json.dumps(s).replace("</", "<\\/")


def _DEV_PAGE(redirect_uri: str, state: str) -> str:
    ru = _js(redirect_uri)
    st = _js(state)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Pinflow dev sign-in</title>
<style>body{{font:14px -apple-system,sans-serif;display:grid;place-items:center;height:100vh;margin:0;background:#0e0e10;color:#f3f3f1}}
.card{{padding:28px 32px;border:1px solid #26262a;border-radius:14px;background:#161618;display:grid;gap:12px;min-width:280px}}
input{{padding:8px 10px;border:1px solid #34343a;border-radius:8px;background:#0e0e10;color:#f3f3f1;font:13px monospace}}
button{{padding:9px 14px;border:0;border-radius:8px;background:#7a9bff;color:#06121f;font-weight:600;cursor:pointer}}
small{{color:#82827a}}</style></head>
<body><form class="card" onsubmit="go(event)">
<b>Pinflow dev sign-in</b>
<small>No Clerk app configured — this stub signs you in as the username below.</small>
<input id="u" value="dev-user" autocomplete="off" spellcheck="false">
<button>Sign in</button></form>
<script>
function go(e){{e.preventDefault();
  var u=document.getElementById('u').value.trim()||'dev-user';
  var url={ru}+'?token='+encodeURIComponent(u)+'&state='+encodeURIComponent({st});
  window.location.href=url;}}
</script></body></html>"""


def _CLERK_PAGE(redirect_uri: str, state: str, pk: str) -> str:
    """Clerk sign-in page. Loads Clerk's browser SDK from CDN (it discovers the
    Frontend API from the publishable key), renders <SignIn>, and hands the default
    session token to /auth/callback.

    Robust to social/OAuth sign-in: Clerk bounces the OAuth handshake to the app
    origin ("/"), which the root route forwards here with the handshake params
    intact — clerk.load() then completes it. redirect_uri/state, dropped by the
    OAuth round-trip, are recovered from sessionStorage (stashed on first visit)."""
    ru, st, pkj = _js(redirect_uri), _js(state), _js(pk)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Sign in to Pinflow</title>
<style>body{{font:14px -apple-system,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0e0e10;color:#f3f3f1}}
#status{{color:#82827a}}</style></head>
<body><div id="app"><span id="status">Loading…</span></div>
<script type="module">
const KEY="pinflow_login_handoff";
let redirectUri={ru}, state={st};
if(redirectUri){{ try{{ sessionStorage.setItem(KEY, JSON.stringify({{redirectUri:redirectUri, state:state}})); }}catch(e){{}} }}
else {{ try{{ const v=JSON.parse(sessionStorage.getItem(KEY)||"{{}}"); redirectUri=v.redirectUri||""; state=v.state||""; }}catch(e){{}} }}
function handoff(t){{ try{{ sessionStorage.removeItem(KEY); }}catch(e){{}} location.href=redirectUri+"?token="+encodeURIComponent(t)+"&state="+encodeURIComponent(state); }}
async function main(){{
  if(!redirectUri){{ document.getElementById("status").textContent="Missing redirect_uri — open this page from the Pinflow app."; return; }}
  const {{Clerk}}=await import("https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.mjs");
  const clerk=new Clerk({pkj});
  await clerk.load();
  if(clerk.user){{ handoff(await clerk.session.getToken()); return; }}
  const s=document.getElementById("status"); if(s) s.remove();
  const back=location.origin+"/auth/clerk";
  clerk.mountSignIn(document.getElementById("app"),{{ forceRedirectUrl:back, fallbackRedirectUrl:back, signUpForceRedirectUrl:back, signUpFallbackRedirectUrl:back }});
  clerk.addListener(async ({{session}})=>{{ if(session) handoff(await session.getToken()); }});
}}
main().catch((e)=>{{ const s=document.getElementById("status"); if(s) s.textContent="Sign-in error: "+e; }});
</script></body></html>"""


def _SIGNOUT_PAGE(pk: str) -> str:
    """Sign out of both Pinflow and Clerk, then show a clean confirmation. Clerk's
    signOut() defaults to redirecting the browser to "/" (the bare API root), so we
    pin its redirectUrl back to this page (?done=1) where we render the message."""
    pkj = _js(pk)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Signed out of Pinflow</title>
<style>body{{font:14px -apple-system,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0e0e10;color:#f3f3f1}}
.card{{padding:30px 34px;border:1px solid #26262a;border-radius:14px;background:#161618;text-align:center;max-width:360px;line-height:1.55}}
.ok{{color:#7ee0a6;font-weight:600;font-size:15px}}
.sub{{color:#9a9a93;margin-top:8px;font-size:13px}}</style></head>
<body><div id="app"><div class="card"><div class="sub">Signing out…</div></div></div>
<script type="module">
const done=new URLSearchParams(location.search).get("done");
function showDone(){{
  document.getElementById("app").innerHTML =
    '<div class="card"><div class="ok">✓ Signed out</div>'
    + '<div class="sub">You can close this tab.</div></div>';
}}
async function main(){{
  if(done){{ showDone(); return; }}
  try{{ await fetch("/auth/logout",{{method:"POST"}}); }}catch(e){{}}
  const {{Clerk}}=await import("https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.mjs");
  const clerk=new Clerk({pkj});
  await clerk.load();
  if(clerk.user){{
    try{{ await clerk.signOut({{ redirectUrl: location.origin + "/auth/signout?done=1" }}); return; }}catch(e){{}}
  }}
  showDone();
}}
main().catch(()=>showDone());
</script></body></html>"""
