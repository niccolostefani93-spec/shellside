"""Routes for the access gate: /auth/* (public, listed in gate.OPEN_PREFIXES)
and /api/access/* (behind the gate like everything else, loopback-only on top
— these change the password, so being logged in from the LAN is not enough).
"""
from __future__ import annotations

import ipaddress
import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import auth, devices
from .config import WEB_DIR, load_settings, save_settings

router = APIRouter()


LOOPBACK_IPS = ("127.0.0.1", "::1")


def _bind_is_loopback_only() -> bool:
    return os.environ.get("SHELLSIDE_BIND_HOST", "127.0.0.1") in LOOPBACK_IPS


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# --------------------------------------------------------- device metadata
#
# Best-effort only, stdlib-only (see auth.py's docstring on why this project
# avoids new dependencies): a handful of substring checks against the
# User-Agent header, good enough to tell devices apart in a list, not meant
# to be authoritative. Order matters — e.g. Edge and Opera both contain
# "Chrome" in their UA string, so they have to be checked first.

def _parse_user_agent(ua: str) -> tuple[str, str]:
    u = ua or ""
    if "Windows" in u:
        os_name = "Windows"
    elif "Android" in u:
        os_name = "Android"
    elif "iPhone" in u or "iPad" in u or "iPod" in u:
        os_name = "iOS"
    elif "Mac OS X" in u or "Macintosh" in u:
        os_name = "macOS"
    elif "Linux" in u:
        os_name = "Linux"
    else:
        os_name = "Unknown OS"

    if "Edg/" in u or "EdgA/" in u or "EdgiOS/" in u:
        browser = "Edge"
    elif "OPR/" in u or "Opera" in u:
        browser = "Opera"
    elif "Firefox/" in u or "FxiOS/" in u:
        browser = "Firefox"
    elif "CriOS/" in u:
        browser = "Chrome"
    elif "Chrome/" in u:
        browser = "Chrome"
    elif "Safari/" in u and "Version/" in u:
        browser = "Safari"
    else:
        browser = "Unknown browser"

    return os_name, browser


def _device_meta(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Everything worth recording about an enrolling client: what the
    server itself can see (IP, User-Agent) plus whatever the client
    volunteered about itself in the enrollment request body — timezone,
    language, and screen size, the closest a browser can honestly get to
    "geographic area" and "what kind of screen this is" without asking for
    location permission the app has no use for otherwise."""
    ua = request.headers.get("user-agent", "")
    os_name, browser = _parse_user_agent(ua)
    client = body.get("client_info") if isinstance(body.get("client_info"), dict) else {}
    return {
        "ip": _client_ip(request),
        "user_agent": ua[:300],
        "os": os_name,
        "browser": browser,
        "timezone": str(client.get("timezone", ""))[:60],
        "language": str(client.get("language", ""))[:20],
        "screen": str(client.get("screen", ""))[:20],
    }


def _is_loopback(request: Request) -> bool:
    # Same rule as the gate: loopback that is not a relayed request (tunnel,
    # reverse proxy) and not a DNS-rebound website. See gate._really_local().
    from .gate import _really_local
    return _really_local(request.scope)


def _is_lan_peer(ip: str) -> bool:
    """True only for a genuine LAN address: a private-range IP that is not
    loopback and not link-local.

    This is deliberately narrower than Python's `is_private` (which also
    counts loopback and link-local as private). The LAN password is meant to
    prove "you're on this network", and neither loopback nor link-local
    proves that:

    - loopback (127.0.0.1 / ::1) is what a same-box process presents as —
      including a tunnel daemon (cloudflared etc.) forwarding traffic from
      the open internet to this app's local port. Trusting loopback here
      would let anyone who reaches the tunnel URL try the LAN password,
      exactly the gap this function exists to close. A genuinely local
      browser doesn't need this endpoint anyway: gate.py's rule 1 already
      lets it in for free (tunnel mode off), and /auth/local is the
      loopback-only recovery path when tunnel mode is on.
    - link-local (169.254.0.0/16, fe80::/10) is what a host gives itself
      when it fails to get a real address; it isn't evidence of being on
      *this* LAN.

    A public/internet address obviously isn't LAN either, and `is_private`
    already returns False for those.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local:
        return False
    return addr.is_private


# One rule for everything: a request that got past gate.py (loopback trust, a
# session cookie, or an enrolled device) can do anything in the app, Settings ›
# Access included. A second, narrower check here used to accept only a
# session cookie, so a phone signed in through its device cookie could open a
# shell but not change a password: a difference that protected nothing, since
# that same shell can edit the files those settings live in.


# ------------------------------------------------------------------- login

@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    html = (WEB_DIR / "login.html").read_text()
    html = html.replace("{{PASSWORD_SET}}", "true" if auth.password_is_set() else "false")
    html = html.replace("{{IS_LOOPBACK}}", "true" if _is_loopback(request) else "false")
    # The LAN password only means something as proof of being on this
    # network — see _is_lan_peer's docstring. Loopback (same box, or a
    # tunnel forwarding as loopback) and public addresses don't qualify, so
    # the field isn't even offered to them; showing it would just invite a
    # password attempt the server is going to reject anyway.
    html = html.replace("{{IS_LAN}}", "true" if _is_lan_peer(_client_ip(request)) else "false")
    # Not shown at all once the internet password has run out of slots or
    # days — no point offering a field that can only ever say "no".
    html = html.replace("{{INTERNET_ACTIVE}}",
                         "true" if auth.internet_password_active() else "false")
    return HTMLResponse(html)


@router.post("/auth/login")
async def login_submit(request: Request) -> JSONResponse:
    if auth.authentications_blocked():
        return JSONResponse({"error": "auth_blocked"}, status_code=423)
    ip = _client_ip(request)
    if auth.ip_blocked(ip):
        return JSONResponse({"error": "ip_blocked"}, status_code=429)
    remaining = auth.throttle_remaining()
    if remaining > 0:
        return JSONResponse({"error": "throttled", "retry_after": remaining},
                            status_code=429)
    auth.register_attempt()

    if not auth.password_is_set():
        return JSONResponse({"error": "no_password"}, status_code=403)

    # The LAN password proves "you're on this network" — a network-origin
    # check, not a credential check, so it doesn't count against the
    # failure counters (same treatment as no_password/no_slots above). A
    # loopback-presenting request (same box, or a tunnel forwarding as
    # loopback) or a public address never gets to try this password at all;
    # see _is_lan_peer's docstring for why loopback doesn't qualify.
    if not _is_lan_peer(ip):
        return JSONResponse({"error": "lan_only"}, status_code=403)

    body = await request.json()
    password = str(body.get("password", ""))
    if not auth.verify_password(password):
        auth.register_failure(ip)
        return JSONResponse({"error": "bad_credentials"}, status_code=401)

    auth.register_success(ip)
    response = JSONResponse({"ok": True})
    auth.issue_cookie(response, via="password", secure=request.url.scheme == "https")
    return response


@router.post("/auth/login-internet")
async def login_internet_submit(request: Request) -> JSONResponse:
    """The enrollment password (server/auth.py): a WAN device trades this,
    once, for its own rotating device credential (server/devices.py) — the
    password itself is never stored anywhere on the client, only the
    device's own token is. Shares the LAN password's throttle/IP-block/
    global-freeze state below: the same box, the same threat model, no
    reason to give an attacker two separate budgets to spend."""
    if auth.authentications_blocked():
        return JSONResponse({"error": "auth_blocked"}, status_code=423)
    ip = _client_ip(request)
    if auth.ip_blocked(ip):
        return JSONResponse({"error": "ip_blocked"}, status_code=429)
    remaining = auth.throttle_remaining()
    if remaining > 0:
        return JSONResponse({"error": "throttled", "retry_after": remaining},
                            status_code=429)
    auth.register_attempt()

    if not auth.internet_password_active():
        return JSONResponse({"error": "not_available"}, status_code=403)

    body = await request.json()
    password = str(body.get("password", ""))
    if not auth.verify_internet_password(password):
        auth.register_failure(ip)
        return JSONResponse({"error": "bad_credentials"}, status_code=401)

    if not auth.consume_internet_slot():
        # Lost a race for the last slot against another device — the
        # password itself was correct, so this doesn't count as a failure.
        return JSONResponse({"error": "no_slots"}, status_code=409)

    auth.register_success(ip)
    label = str(body.get("label", "") or "Web device")[:60]
    meta = _device_meta(request, body)
    device_id, token = devices.create(label=label, meta=meta)
    secure = request.url.scheme == "https"

    response = JSONResponse({"ok": True, "device_id": device_id})
    auth.issue_cookie(response, via="device", device_id=device_id, secure=secure)
    devices.issue_cookie(response, device_id, token, secure)
    return response


@router.post("/auth/logout")
async def logout() -> JSONResponse:
    """Clears the session, and — this is the part that used to be missing —
    suspends gate-level loopback trust too, via auth.set_locked(True).

    Without that second part, signing out on the single most common
    deployment (bound to loopback, no LAN password) accomplished nothing:
    the very next request from that same loopback client would walk straight
    back in, because gate-level trust never needed the cookie this endpoint
    clears. The lock is what makes "sign out" actually mean "the next visit
    needs to prove it again," regardless of which trust rule would otherwise
    have applied. It lifts itself the moment any real login succeeds — see
    auth.issue_cookie().
    """
    response = JSONResponse({"ok": True})
    auth.clear_cookie(response)
    auth.set_locked(True)
    return response


# --------------------------------------------------------- local recovery

@router.get("/auth/local")
async def auth_local_get(request: Request, token: str = "") -> Any:
    if not _is_loopback(request):
        return RedirectResponse("/auth/login")
    if not auth.consume_local_token(token):
        return RedirectResponse("/auth/login?admin=1")
    response = RedirectResponse("/")
    auth.issue_cookie(response, via="local", secure=request.url.scheme == "https")
    return response


@router.post("/auth/local")
async def auth_local_post(request: Request) -> JSONResponse:
    if not _is_loopback(request):
        return JSONResponse({"error": "not_local"}, status_code=403)
    body = await request.json()
    token = str(body.get("token", ""))
    if not auth.consume_local_token(token):
        return JSONResponse({"error": "bad_token"}, status_code=401)
    response = JSONResponse({"ok": True})
    auth.issue_cookie(response, via="local", secure=request.url.scheme == "https")
    return response


# --------------------------------------------------------- admin (Settings)

def _recovery_url() -> str:
    port = os.environ.get("SHELLSIDE_PORT", "8022")
    return f"http://127.0.0.1:{port}/auth/local?token={auth.local_token()}"


@router.get("/api/access/status")
async def access_status(request: Request) -> dict[str, Any]:
    return {
        "password_set": auth.password_is_set(),
        "lan_open": auth.lan_open(),
        "locked": auth.locked(),
        "bind_loopback_only": os.environ.get("SHELLSIDE_BIND_HOST", "127.0.0.1")
                              in LOOPBACK_IPS,
        # What's actually running right now vs. what's saved and takes effect
        # on the next restart — these can briefly disagree right after a
        # bind-host change, while the restart is still in flight.
        "bind_host_live": os.environ.get("SHELLSIDE_BIND_HOST", "127.0.0.1"),
        "bind_host_saved": load_settings().get("bind_host", "127.0.0.1"),
        "recovery_url": _recovery_url(),
        "throttle_seconds": auth.setting("throttle_seconds"),
        "block_ip_after": auth.setting("block_ip_after"),
        "block_auth_after": auth.setting("block_auth_after"),
        "block_authentications": auth.authentications_blocked(),
        "tunnel_mode": auth.tunnel_mode(),
        "internet_password": auth.internet_password_state(),
    }


# ------------------------------------------------------- internet password

@router.post("/api/access/internet-password")
async def access_set_internet_password(request: Request, payload: dict[str, Any]) -> dict:
    password = str(payload.get("password", ""))
    if not password:
        auth.clear_internet_password()
        return {"ok": True, "internet_password": auth.internet_password_state()}
    slots = payload.get("slots", 1)
    days = payload.get("days", 7)
    try:
        slots, days = int(slots), float(days)
    except (TypeError, ValueError):
        raise HTTPException(400, "slots has to be an integer, days a number.")
    auth.set_internet_password(password, slots, days)
    return {"ok": True, "internet_password": auth.internet_password_state(),
            "tunnel_mode": auth.tunnel_mode()}


@router.post("/api/access/internet-password/revoke")
async def access_revoke_internet_password(request: Request) -> dict:
    """Kills the internet password immediately, however many slots or days
    it had left — distinct from letting it run out on its own. Devices
    already enrolled through it are untouched; they carry their own
    rotating token (server/devices.py) and were never contingent on this
    password staying valid."""
    auth.clear_internet_password()
    return {"ok": True, "internet_password": auth.internet_password_state()}


@router.post("/api/access/tunnel-mode")
async def access_tunnel_mode(request: Request, payload: dict[str, Any]) -> dict:
    auth.set_tunnel_mode(bool(payload.get("value", False)))
    return {"ok": True, "tunnel_mode": auth.tunnel_mode()}


# ------------------------------------------------------------------ devices

@router.get("/api/access/devices")
async def access_devices(request: Request) -> dict:
    my = devices.read_cookie(request.cookies)
    out = [
        {"id": device_id, "label": rec.get("label"), "created": rec.get("created"),
         "last_seen": rec.get("last_seen"), "revoked": rec.get("revoked", False),
         "meta": rec.get("meta", {}),
         "is_this_device": bool(my and my["device_id"] == device_id)}
        for device_id, rec in devices.all_devices().items()
    ]
    out.sort(key=lambda d: d["last_seen"], reverse=True)
    return {"devices": out}


@router.post("/api/access/devices/revoke")
async def access_devices_revoke(request: Request, payload: dict[str, Any]) -> dict:
    device_id = str(payload.get("device_id", ""))
    ok = devices.revoke(device_id)
    return {"ok": ok}


@router.post("/api/access/devices/revoke-all")
async def access_devices_revoke_all(request: Request) -> dict:
    return {"ok": True, "revoked": devices.revoke_all()}


@router.post("/api/access/devices/rename")
async def access_devices_rename(request: Request, payload: dict[str, Any]) -> dict:
    ok = devices.rename(str(payload.get("device_id", "")), str(payload.get("label", "")))
    return {"ok": ok}


@router.post("/api/access/password")
async def access_set_password(request: Request, payload: dict[str, Any]) -> dict:
    password = str(payload.get("password", ""))
    if not password:
        auth.clear_password()
        return {"ok": True, "password_set": False}
    auth.set_password(password)
    return {"ok": True, "password_set": True}


@router.post("/api/access/lan-open")
async def access_lan_open(request: Request, payload: dict[str, Any]) -> dict:
    auth.set_setting("lan_open", bool(payload.get("value", False)))
    return {"ok": True, "lan_open": auth.lan_open()}


@router.post("/api/network/bind")
async def network_bind(request: Request, payload: dict[str, Any]) -> dict:
    """Change which address the app binds to, then restart to apply it.

    This writes settings.json's bind_host (in DATA_HOME) and exits — run.py's
    supervisor re-reads that file on every loop pass (see run.py's
    resolve_host()) and rebinds to whatever it finds there. Nothing here
    talks to the supervisor directly; the file is the entire handoff.

    Loopback-only, same as a password change: this is the actual network
    exposure switch, so it gets at least as strict a gate as the thing it
    controls access to, not a looser one.

    A restart drops every open shell — the caller is expected to have
    already shown that cost and gotten it confirmed, the same as the update
    flow's own "sessions at risk" preview. This endpoint does not ask again.
    """
    host = str(payload.get("host", "")).strip()
    if host not in ("127.0.0.1", "0.0.0.0"):
        raise HTTPException(400, "host has to be '127.0.0.1' or '0.0.0.0'.")

    current = load_settings().get("bind_host", "127.0.0.1")
    if host == current:
        return {"ok": True, "bind_host": host, "restarting": False}

    save_settings({"bind_host": host})

    # Importing server.main here (rather than at module load) avoids a
    # circular import — main.py already imports this router.
    from .main import _schedule_restart
    _schedule_restart()
    return {"ok": True, "bind_host": host, "restarting": True}


@router.post("/api/access/logout-everywhere")
async def access_logout_everywhere(request: Request) -> dict:
    auth.bump_session_epoch()
    return {"ok": True}


@router.post("/api/access/unblock")
async def access_unblock(request: Request) -> dict:
    auth.set_setting("block_authentications", False)
    auth.reset_lockouts()
    return {"ok": True}


@router.post("/api/access/limits")
async def access_limits(request: Request, payload: dict[str, Any]) -> dict:
    for key in ("throttle_seconds", "block_ip_after", "block_auth_after"):
        if key in payload:
            try:
                auth.set_setting(key, max(1, int(payload[key])))
            except (TypeError, ValueError):
                pass
    return {"ok": True}
