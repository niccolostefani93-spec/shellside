"""The access gate — unconditional, not a swappable service.

This exists because of a mistake in an earlier version of this app: security was
a plugin selected by services/security/active.json, and the request handler
fell back to *allow* whenever that plugin failed to load. A missing folder, a
typo in active.json, a service that raised on import — any of those silently
turned off every check in the app, and nothing said so.

So this module is imported and wired into server/gate.py unconditionally, in
main.py, the same way the PTY and the filesystem view are. There is no
active.json for this. There is no "disable" switch other than deliberately
choosing an empty LAN password (see lan_open below), which is a decision this
module makes you take on purpose, not a state you can arrive at by accident.

Everything here is stdlib only — hmac, hashlib, secrets, json. No new
dependency, on purpose: this project has already cost its owner one afternoon
chasing a half-installed package, and the access gate is the last place that
should need another.

Two authenticated "via" a session can carry:
  password  the LAN password (see below) — subject to the session epoch, so
            changing the password or pressing "log out everywhere" drops it
  local     the proof-of-locality token, consumed once from loopback — never
            subject to the epoch, because it is how you recover from a lost
            password in the first place

Storage lives under DATA_HOME/auth/ (see server/datahome.py), mode 0700,
files mode 0600 — outside the code directory, which an update rewrites, and
left out of the settings backup zip.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .config import AUTH_DIR
COOKIE_NAME = "shellside_session"
SESSION_MAX_AGE = 30 * 24 * 3600          # 30 days: this runs a phone-reachable
                                           # terminal, not a banking site.
LOCAL_TOKEN_PATH_HINT = "/auth/local?token="

DEFAULTS = {
    "throttle_seconds": 3,       # minimum gap between any two login attempts
    "block_ip_after": 5,         # failures from one IP before it is locked out
    "block_ip_minutes": 15,
    "block_auth_after": 20,      # failures overall before login is frozen
                                  # entirely, until cleared from loopback
    "block_authentications": False,
    "lan_open": False,           # deliberate, explicit "no password on the LAN"
    "tunnel_mode": False,        # see tunnel_mode() below
}


# --------------------------------------------------------------------- store

def _ensure_dir() -> Path:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AUTH_DIR, 0o700)
    except OSError:
        pass
    return AUTH_DIR


def _read(name: str, default: Any) -> Any:
    path = AUTH_DIR / name
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, data: Any) -> None:
    _ensure_dir()
    path = AUTH_DIR / name
    path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _auth() -> dict:
    return _read("auth.json", {})


def _save_auth(data: dict) -> None:
    _write("auth.json", data)


def setting(key: str):
    return _auth().get(key, DEFAULTS.get(key))


def set_setting(key: str, value) -> None:
    data = _auth()
    data[key] = value
    _save_auth(data)


# ------------------------------------------------------------- signing key

_key_cache: bytes | None = None


def _signing_key() -> bytes:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    path = AUTH_DIR / "secret_key"
    if path.is_file():
        raw = path.read_text().strip()
        if raw:
            _key_cache = bytes.fromhex(raw)
            return _key_cache
    raw = secrets.token_bytes(32)
    _ensure_dir()
    path.write_text(raw.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _key_cache = raw
    return raw


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign(payload: dict) -> str:
    """A signed, timestamped token. Not encrypted — nothing in the payload is
    secret, it only must not be forgeable. Format: base64(json).base64(sig)."""
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def unsign(token: str, max_age: int) -> dict | None:
    """Verify and decode. None if malformed, tampered, or expired."""
    try:
        body, signature_b64 = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
    try:
        given = _unb64(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, given):
        return None
    # base64 leaves unused low bits in the last character, so several
    # spellings decode to the same signature; accept only the one sign() emits.
    if not hmac.compare_digest(_b64(given), signature_b64):
        return None
    try:
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if time.time() - float(payload.get("iat", 0)) > max_age:
        return None
    return payload


# --------------------------------------------------------------- password
#
# Hashed, not stored in the clear. hashlib.scrypt is stdlib, so this costs
# nothing extra to depend on, and it means a forgotten password is a reset
# (via the local token below) rather than something anyone with read access
# to the auth/ folder could simply read back.

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def password_is_set() -> bool:
    return bool(_auth().get("password_hash"))


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    data = _auth()
    data["password_hash"] = _b64(digest)
    data["password_salt"] = _b64(salt)
    for key in ("bootstrap_hash", "bootstrap_salt", "bootstrap_expires_at"):
        data.pop(key, None)       # a real password retires the bootstrap one
    _save_auth(data)
    bump_session_epoch()          # changing the password drops other sessions


def verify_password(password: str) -> bool:
    data = _auth()
    stored_hash, stored_salt = data.get("password_hash"), data.get("password_salt")
    if not stored_hash or not stored_salt:
        return False
    digest = hashlib.scrypt(password.encode(), salt=_unb64(stored_salt),
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return hmac.compare_digest(_b64(digest), stored_hash)


def clear_password() -> None:
    data = _auth()
    data.pop("password_hash", None)
    data.pop("password_salt", None)
    _save_auth(data)
    bump_session_epoch()


# ------------------------------------------------------ bootstrap password
#
# The setup-time counterpart of the proof-of-locality token below. That token
# only works from this machine; this one lets a *second device on the LAN*
# finish first login on a headless box, where there is no local browser.
#
# Issued by `run.py setup` (server/wizard.py), printed once, stored only as a
# hash. It is a fallback with a short life:
#   - accepted only while no real LAN password exists; once one does, it is
#     ignored and fails like any wrong password;
#   - expires BOOTSTRAP_MINUTES after it was issued, used or not;
#   - consumed by its first successful login, which can do nothing except
#     choose the real password (see auth_routes.py's /auth/first-password);
#   - LAN only: never through a tunnel or proxy, never as an internet password.

BOOTSTRAP_MINUTES = 30
BOOTSTRAP_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BOOTSTRAP_LENGTH = 8
FIRST_PASSWORD_SECONDS = 15 * 60       # how long a bootstrap login may take
                                       # to choose the real password
MIN_PASSWORD_LENGTH = 8


def _scrypt(password: str, salt: bytes) -> str:
    return _b64(hashlib.scrypt(password.encode(), salt=salt,
                               n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32))


def issue_bootstrap_password(minutes: float = BOOTSTRAP_MINUTES) -> str | None:
    """A new bootstrap password, or None when a real password already exists
    (re-running setup must never weaken a configured install)."""
    if password_is_set():
        return None
    password = "".join(secrets.choice(BOOTSTRAP_ALPHABET) for _ in range(BOOTSTRAP_LENGTH))
    salt = secrets.token_bytes(16)
    data = _auth()
    data["bootstrap_hash"] = _scrypt(password, salt)
    data["bootstrap_salt"] = _b64(salt)
    data["bootstrap_expires_at"] = time.time() + minutes * 60
    _save_auth(data)
    return password


def bootstrap_active() -> bool:
    data = _auth()
    return (bool(data.get("bootstrap_hash"))
            and not data.get("password_hash")
            and time.time() < float(data.get("bootstrap_expires_at", 0)))


def bootstrap_expires_at() -> float:
    return float(_auth().get("bootstrap_expires_at", 0)) if bootstrap_active() else 0.0


def verify_bootstrap_password(password: str) -> bool:
    if not bootstrap_active():
        return False
    data = _auth()
    return hmac.compare_digest(
        _scrypt(password, _unb64(data["bootstrap_salt"])), data["bootstrap_hash"])


def clear_bootstrap_password() -> None:
    data = _auth()
    for key in ("bootstrap_hash", "bootstrap_salt", "bootstrap_expires_at"):
        data.pop(key, None)
    _save_auth(data)


def reset_access() -> None:
    """Back to the unconfigured state: no LAN password, no bootstrap password,
    LAN-without-password off, every session signed out. Devices enrolled
    through the internet password are left alone; revoke them in Settings."""
    data = _auth()
    for key in ("password_hash", "password_salt",
                "bootstrap_hash", "bootstrap_salt", "bootstrap_expires_at"):
        data.pop(key, None)
    data["lan_open"] = False
    _save_auth(data)
    bump_session_epoch()


def lan_exposure_allowed() -> bool:
    """May the server listen on a non-loopback address?

    Only when something protects it there, or the owner deliberately chose
    not to (lan_open): a real LAN password, an unexpired bootstrap password,
    or lan_open. run.py checks the same rule on auth.json directly, because
    it must work before server/ is importable."""
    return password_is_set() or bootstrap_active() or lan_open()


# ------------------------------------------------------ internet password
#
# A second, deliberately separate secret from the LAN password above — same
# hashing, same storage discipline, but with a use-count and an expiry that
# the LAN password doesn't have. Its only job is to hand out device
# credentials (see server/devices.py) to machines arriving over the
# internet: it is not meant to be typed every day, so once its slots or its
# days run out, it stops authenticating anything — the devices it already
# enrolled keep working on their own rotating token, unaffected.
#
# Nothing here is deleted when it runs out; it just stops verifying (see
# internet_password_active below), so Settings can still show "2/2 used,
# expired Sep 3" instead of the field just going blank with no explanation.

def internet_password_state() -> dict:
    data = _auth()
    return {
        "set": bool(data.get("internet_password_hash")),
        "slots_total": int(data.get("internet_slots_total", 0)),
        "slots_used": int(data.get("internet_slots_used", 0)),
        "expires_at": float(data.get("internet_expires_at", 0)),
        "created_at": float(data.get("internet_created_at", 0)),
        "active": internet_password_active(),
    }


def internet_password_active() -> bool:
    data = _auth()
    if not data.get("internet_password_hash"):
        return False
    if data.get("internet_slots_used", 0) >= data.get("internet_slots_total", 0):
        return False
    if time.time() >= data.get("internet_expires_at", 0):
        return False
    return True


def set_internet_password(password: str, slots: int, days: float) -> None:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    data = _auth()
    data["internet_password_hash"] = _b64(digest)
    data["internet_password_salt"] = _b64(salt)
    data["internet_slots_total"] = max(1, int(slots))
    data["internet_slots_used"] = 0
    data["internet_created_at"] = time.time()
    data["internet_expires_at"] = time.time() + max(1.0, float(days)) * 86400
    _save_auth(data)
    if not setting("tunnel_mode"):        # see tunnel_mode()'s docstring
        set_setting("tunnel_mode", True)


def clear_internet_password() -> None:
    data = _auth()
    for key in ("internet_password_hash", "internet_password_salt",
                "internet_slots_total", "internet_slots_used",
                "internet_created_at", "internet_expires_at"):
        data.pop(key, None)
    _save_auth(data)


def verify_internet_password(password: str) -> bool:
    if not internet_password_active():
        return False
    data = _auth()
    stored_hash, stored_salt = data.get("internet_password_hash"), data.get("internet_password_salt")
    if not stored_hash or not stored_salt:
        return False
    digest = hashlib.scrypt(password.encode(), salt=_unb64(stored_salt),
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return hmac.compare_digest(_b64(digest), stored_hash)


def consume_internet_slot() -> bool:
    """Decrements the slot counter; False if none were left (lost a race
    against another device claiming the last one). Not a hard atomic op —
    this is a single-process app and the window is one dict read/write, the
    same trade-off the rest of this module already makes."""
    data = _auth()
    used = data.get("internet_slots_used", 0)
    total = data.get("internet_slots_total", 0)
    if used >= total:
        return False
    data["internet_slots_used"] = used + 1
    _save_auth(data)
    return True


def lan_open() -> bool:
    """True only when explicitly toggled on in Settings.

    Deliberately not inferred from 'no password set' — first boot has no
    password set either, and that must fail closed, not open. Turning the LAN
    password off is a choice you make with a switch, not one you arrive at by
    leaving a field empty.
    """
    return bool(setting("lan_open"))


# ------------------------------------------------------------- tunnel mode
#
# gate.py's rule 1 trusts a loopback-presenting client outright when the app
# is bound to 127.0.0.1 only — correct for "you, at this machine", but a
# cloudflared tunnel running on the same box also forwards as loopback, so
# on its own that rule would hand out full access to anyone who reaches the
# tunnel URL, no password ever asked. Turning this on tells the gate that
# loopback is no longer proof of anything: every request, tunnel included,
# must carry a real session or device credential. /auth/local (the
# proof-of-locality recovery token) is unaffected — it already checks
# client-is-loopback itself, in auth_routes.py, independently of this.
#
# Turned on automatically the first time an internet password is set (see
# set_internet_password below), because setting one is exactly the signal
# that this box is meant to be reached from outside. It stays a real
# setting, not a hidden side effect: visible and toggleable from Settings
# for anyone who wants it off.

def tunnel_mode() -> bool:
    return bool(setting("tunnel_mode"))


def set_tunnel_mode(value: bool) -> None:
    set_setting("tunnel_mode", bool(value))


# ---------------------------------------------------------------- lock flag

def locked() -> bool:
    """True after a deliberate sign-out; suspends gate-level loopback trust.

    Gate-level trust (see gate.py's _trusted()) is what lets a personal,
    loopback-bound install skip a password entirely — correct for the common
    case, but it also means a plain cookie-clear on /auth/logout accomplishes
    nothing there: the very next request walks straight back in with no
    cookie needed in the first place. This flag is the actual "require proof
    again" switch that a sign-out needs in that mode.

    Persisted (not in-memory, unlike the rate-limit counters below) because
    the point is exactly to survive until someone deliberately proves who
    they are — including across a restart, if the next visit is that much
    later. Cleared the moment any real login succeeds, whichever door it came
    through: the LAN password, or the local recovery token, since the
    recovery token IS the physical-access escape hatch and must always be
    able to clear this, not be blocked by it.
    """
    return bool(setting("locked"))


def set_locked(value: bool) -> None:
    set_setting("locked", value)


# ----------------------------------------------------------- session epoch

def session_epoch() -> int:
    try:
        return int(setting("session_epoch") or 0)
    except (TypeError, ValueError):
        return 0


def bump_session_epoch() -> None:
    set_setting("session_epoch", session_epoch() + 1)


# ------------------------------------------------------------ session cookie

def issue_cookie(response, *, via: str, device_id: str | None = None,
                 secure: bool = False) -> None:
    """Set the session cookie. `secure` when the browser reached us over
    HTTPS (directly, or through a proxy such as cloudflared that says so), so
    the cookie is never sent over plain HTTP afterwards."""
    payload = {"via": via, "iat": time.time(), "ep": session_epoch()}
    if device_id:
        payload["device_id"] = device_id
    response.set_cookie(
        COOKIE_NAME, sign(payload), max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax", path="/", secure=secure,
    )
    set_locked(False)   # a real login always answers a pending "prove it again"


def read_session(cookies: dict[str, str]) -> dict | None:
    raw = cookies.get(COOKIE_NAME)
    if not raw:
        return None
    payload = unsign(raw, SESSION_MAX_AGE)
    if payload is None:
        return None
    if payload.get("via") != "local" and int(payload.get("ep", -1)) != session_epoch():
        return None            # password changed, or "log out everywhere" pressed
    return payload


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# ------------------------------------------------------ proof of locality
#
# Rewritten every boot, printed to the console, never expires during that
# boot's lifetime. This is the actual safety net: whatever else goes wrong —
# forgotten password, a bad Settings change, a typo in the LAN password field
# — sitting at the machine and reading its own terminal always gets you back
# in. It cannot be phished or replayed from another boot, because it dies the
# moment the process restarts.

def new_local_token() -> str:
    token = secrets.token_urlsafe(32)
    _write("local_token.json", {"token": token, "created": time.time()})
    return token


def local_token() -> str:
    return _read("local_token.json", {}).get("token", "")


def consume_local_token(token: str) -> bool:
    current = local_token()
    if not token or not current:
        return False
    return hmac.compare_digest(current, token)


# ------------------------------------------------------------- rate limiting
#
# In-memory, reset on restart, on purpose: this is a home appliance, not a
# service worth persisting attacker bookkeeping for across reboots.

_ip_fails: dict[str, dict] = {}
_global_fails = 0
_last_attempt = 0.0


def ip_blocked(ip: str) -> bool:
    record = _ip_fails.get(ip)
    return bool(record and record["until"] > time.time())


def throttle_remaining() -> float:
    gap = float(setting("throttle_seconds"))
    remaining = gap - (time.time() - _last_attempt)
    return max(0.0, remaining)


def register_attempt() -> None:
    global _last_attempt
    _last_attempt = time.time()


def register_failure(ip: str) -> None:
    global _global_fails
    record = _ip_fails.setdefault(ip, {"fails": 0, "until": 0.0})
    record["fails"] += 1
    _global_fails += 1
    if record["fails"] >= int(setting("block_ip_after")):
        record["until"] = time.time() + int(setting("block_ip_minutes")) * 60
    if _global_fails >= int(setting("block_auth_after")) and not setting("block_authentications"):
        set_setting("block_authentications", True)


def register_success(ip: str) -> None:
    global _global_fails
    _ip_fails.pop(ip, None)
    _global_fails = 0


def reset_lockouts() -> None:
    global _global_fails
    _ip_fails.clear()
    _global_fails = 0


def authentications_blocked() -> bool:
    return bool(setting("block_authentications"))
