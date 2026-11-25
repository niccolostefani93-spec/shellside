"""Per-device rotating credential — layered on top of auth.py's password login.

auth.py's session cookie proves "this browser presented a valid password
recently"; every device that logs in the same way shares the same kind of
proof, and the only way to invalidate one is auth.bump_session_epoch(),
which invalidates *all* of them. That is fine for the LAN password's normal
use, but not for devices enrolled remotely (see auth.py's internet
password): if one of those is later lost or its cookie leaks, you want to
revoke that one machine, not sign everyone out.

So each device that enrolls (server/auth_routes.py's /auth/login-internet)
gets its own secret here instead — a 32-byte token, opaque to the device
itself; the server keeps only a hash of it (never the token), in two slots:

    cur   the token this device is expected to present next
    prev  the token it was still allowed to present a moment ago

Every grant rotates: present `cur`, get handed a fresh token and the old
`cur` slides into `prev`. That grace slot exists because a page load fires
several requests near-simultaneously — without it, the second of two
concurrent requests would look like reuse of an already-rotated token and
get revoked for no reason. ROTATE_COOLDOWN caps how often a rotation
actually happens, for the same reason.

Presenting a token that is neither `cur` nor `prev` means the one in this
cookie was already superseded and is being replayed — the only way that
happens is two copies of the same cookie being used from two places, which
is exactly what a stolen cookie looks like. That revokes the device
outright, no cascade: there is no device tree here, so there is nothing
downstream of it to take down too.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from . import auth

DEVICES_FILE = "devices.json"
DEVICE_COOKIE = "shellwalk_device"
DEVICE_MAX_AGE = 60 * 60 * 24 * 180        # 180 days, same reasoning as the
                                            # session cookie: this is a home
                                            # appliance, not a bank.
ROTATE_COOLDOWN = 60                       # seconds; see module docstring


# ------------------------------------------------------------------- store

def _read() -> dict[str, dict]:
    return auth._read(DEVICES_FILE, {})


def _write(data: dict[str, dict]) -> None:
    auth._write(DEVICES_FILE, data)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(label: str = "", meta: dict[str, Any] | None = None) -> tuple[str, str]:
    """A brand new device. Returns (device_id, token) — the token is only
    ever returned here and at rotation time; it is never stored in the
    clear, and never logged.

    `meta` is whatever server/auth_routes.py could tell about the enrolling
    client at the moment it enrolled — IP, parsed OS/browser, and anything
    the client volunteered about itself (timezone, language, screen size).
    It is a snapshot, not a live property: a device's browser or network
    can change after enrollment without this being updated, so it should be
    read as "what it looked like when it signed up", not "what it is now".
    """
    device_id = secrets.token_urlsafe(9)
    token = secrets.token_urlsafe(32)
    now = time.time()
    data = _read()
    data[device_id] = {
        "label": (label or "Device")[:60],
        "cur": _hash(token), "prev": None,
        "created": now, "rotated": now, "last_seen": now,
        "revoked": False,
        "meta": meta or {},
    }
    _write(data)
    return device_id, token


def all_devices() -> dict[str, dict]:
    return _read()


def get(device_id: str) -> dict | None:
    return _read().get(device_id)


def revoke(device_id: str) -> bool:
    data = _read()
    rec = data.get(device_id)
    if not rec:
        return False
    rec["revoked"] = True
    _write(data)
    return True


def revoke_all() -> int:
    data = _read()
    count = 0
    for rec in data.values():
        if not rec.get("revoked"):
            rec["revoked"] = True
            count += 1
    _write(data)
    return count


def rename(device_id: str, label: str) -> bool:
    data = _read()
    rec = data.get(device_id)
    if not rec:
        return False
    rec["label"] = (label or rec["label"])[:60]
    _write(data)
    return True


# ------------------------------------------------------------------ grant

def try_grant(device_id: str, token: str) -> tuple[str, str | None]:
    """("ok", None)        trusted, nothing to change on the client
       ("ok", new_token)   trusted, caller must set the new device cookie
       ("revoked", None)   not trusted — missing, revoked, or reused; the
                            caller should clear the device cookie
    """
    data = _read()
    rec = data.get(device_id)
    if not rec or rec.get("revoked"):
        return "revoked", None

    h = _hash(token)
    now = time.time()

    if h == rec["cur"]:
        if now - rec["rotated"] < ROTATE_COOLDOWN:
            rec["last_seen"] = now
            _write(data)
            return "ok", None
        new_token = secrets.token_urlsafe(32)
        rec["prev"] = rec["cur"]
        rec["cur"] = _hash(new_token)
        rec["rotated"] = rec["last_seen"] = now
        _write(data)
        return "ok", new_token

    if rec.get("prev") and h == rec["prev"]:
        rec["last_seen"] = now
        _write(data)
        return "ok", None

    # Neither slot matches: this token was already superseded and is being
    # replayed from somewhere else. Revoke — do not try to guess which copy
    # is the legitimate one, since there is no way to tell from here.
    rec["revoked"] = True
    _write(data)
    print(f"  ⚠ [devices] reuse-detection on device {device_id} "
          f"({rec.get('label')}) — revoked.")
    return "revoked", None


# -------------------------------------------------------------- cookie i/o
#
# The cookie value is auth.sign()'d (HMAC, same signing key as the session
# cookie) so a client can't hand back a device_id/token pair that doesn't
# match — not that guessing a 32-byte token is remotely feasible, but
# signing costs nothing extra given auth.py already has the machinery.

def _cookie_value(device_id: str, token: str) -> str:
    return auth.sign({"device_id": device_id, "token": token, "iat": time.time()})


def read_cookie(cookies: dict[str, str]) -> dict[str, str] | None:
    raw = cookies.get(DEVICE_COOKIE)
    if not raw:
        return None
    payload = auth.unsign(raw, DEVICE_MAX_AGE)
    if not payload:
        return None
    device_id, token = payload.get("device_id"), payload.get("token")
    if not device_id or not token:
        return None
    return {"device_id": device_id, "token": token}


def _attrs(secure: bool) -> str:
    parts = [f"Max-Age={DEVICE_MAX_AGE}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def set_cookie_header(device_id: str, token: str, secure: bool) -> str:
    return f"{DEVICE_COOKIE}={_cookie_value(device_id, token)}; {_attrs(secure)}"


def clear_cookie_header() -> str:
    return f"{DEVICE_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"


def issue_cookie(response: Any, device_id: str, token: str, secure: bool) -> None:
    """For use in ordinary route handlers (Starlette/FastAPI Response)."""
    response.headers.append("set-cookie", set_cookie_header(device_id, token, secure))
