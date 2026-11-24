"""The gate itself — an ASGI middleware wrapping the whole app.

Unlike a route-level check, this runs in front of *everything*: HTTP, static
files, and every websocket, before any of them execute a line of route code.
A new route added later is covered automatically; there is nothing to
remember to add a check to.

Trust, in order:

  1. loopback, and the app is bound to 127.0.0.1 only, and not locked,
     and tunnel mode is off
     -> trusted outright, no cookie needed.
     Nothing but a process on this machine — including your own cloudflared
     tunnel, which forwards here — can ever present as loopback in that case.
     A deliberate sign-out (see auth.locked()) suspends this rule until the
     next real login, so "sign out" means something even in the deployment
     mode that would otherwise make it a no-op. Tunnel mode (see
     auth.tunnel_mode()) suspends it for as long as it's on, precisely
     because a cloudflared tunnel forwarding to this same loopback address
     is the case where "presents as loopback" stops meaning "is you".

  2. a valid session cookie
     -> trusted. Issued by /auth/login (LAN password), /auth/login-internet
     (the internet password, loopback-independent — see auth.py), or
     /auth/local (proof-of-locality token, loopback only). A session issued
     by device enrollment carries that device's id and is only trusted here
     while the device itself is still live — revoking the device revokes
     this cookie too, not just its separate device token below.

  3. a valid device cookie (server/devices.py)
     -> trusted. Issued the first time a device enrolls via the internet
     password. Checked after the session cookie, not instead of it, so a
     device that also happens to hold a live session cookie doesn't rotate
     its device token on every request for no reason. A successful grant
     here may rotate the device's token, which means the response needs an
     extra Set-Cookie header — see _RotatingSend below.

  4. none of the above
     -> denied. HTTP page loads redirect to /auth/login; API calls get 401;
     websockets are closed before they ever reach a route.

Rule 1 deliberately stops applying once the app is bound to 0.0.0.0, once a
sign-out has set the lock flag, or once tunnel mode is on: at that point a
local port-forward, another process on the machine, or a tunnel forwarding
from the open internet can all present as loopback too, and loopback stops
being sufficient on its own. See server/auth.py's module docstring for the
longer version of this reasoning.
"""
from __future__ import annotations

import os

from starlette.responses import JSONResponse, RedirectResponse

from . import assist, auth, devices

OPEN_PREFIXES = ("/auth", "/static")
LOOPBACK_IPS = ("127.0.0.1", "::1")
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

# Headers a reverse proxy or tunnel adds. A request carrying any of them came
# from somewhere else and was only relayed by a process on this machine.
PROXY_HEADERS = (b"cf-connecting-ip", b"cf-ray", b"x-forwarded-for", b"x-forwarded-host",
                 b"x-forwarded-proto",
                 b"x-real-ip", b"forwarded")

UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _host_name(host: str) -> str:
    """'example.com:8022' -> 'example.com', '[::1]:8022' -> '[::1]'."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _same_origin(scope) -> bool:
    """The request was started by one of our own pages.

    Browsers always send Origin on websocket handshakes and on cross-site
    POSTs, and a page cannot forge it. Without this check any website you
    visit could open /ws/terminal on your Shellside: cookies (or loopback
    trust) would come along on their own. That is Cross-Site WebSocket
    Hijacking, and SameSite=Lax does not stop it from a sibling subdomain.

    A request with no Origin is not from a browser page (curl, the assist
    client), so it is not a cross-site attack and is left to the other checks;
    except a websocket, which every browser opens with an Origin.
    """
    origin = _header(scope, b"origin")
    if origin is None:
        return scope["type"] != "websocket"
    if origin == "null":
        return False
    authority = origin.split("://", 1)[-1].rstrip("/").lower()
    # The browser's address, as the request itself says it was addressed. A
    # proxy that rewrites Host (cloudflared's httpHostHeader) usually keeps
    # the original in X-Forwarded-Host.
    for name in (b"host", b"x-forwarded-host"):
        value = _header(scope, name)
        if value and authority == value.split(",")[0].strip().lower():
            return True
    # Addresses listed by hand in settings.json (in DATA_HOME), for setups where neither
    # header carries the public name.
    from .config import load_settings
    allowed = load_settings().get("allowed_origins") or []
    return origin.rstrip("/").lower() in {str(item).rstrip("/").lower() for item in allowed}


def _needs_origin_check(scope) -> bool:
    return scope["type"] == "websocket" or scope.get("method", "GET") in UNSAFE_METHODS


def _really_local(scope) -> bool:
    """Loopback that is genuinely this machine's own browser.

    Two ways traffic from elsewhere can arrive from 127.0.0.1:
    - a tunnel or reverse proxy on this machine (cloudflared) relays it; it
      then carries proxy headers, whatever tunnel mode is set to;
    - DNS rebinding: a website re-points its own name at 127.0.0.1, so its
      page talks to us from "loopback", with an Origin that even matches the
      Host it sends. The Host is still the website's name, never localhost.
    """
    if not _client_is_loopback(scope):
        return False
    if any(name in PROXY_HEADERS for name, _ in scope.get("headers", ())):
        return False
    host = _header(scope, b"host")
    return host is not None and _host_name(host) in LOOPBACK_HOSTS


def _bind_is_loopback_only() -> bool:
    return os.environ.get("SHELLSIDE_BIND_HOST", "127.0.0.1") in LOOPBACK_IPS


def _client_is_loopback(scope) -> bool:
    client = scope.get("client")
    return bool(client) and client[0] in LOOPBACK_IPS


def _cookies(scope) -> dict[str, str]:
    for name, value in scope.get("headers", ()):
        if name == b"cookie":
            jar = {}
            for part in value.decode("latin-1").split(";"):
                if "=" in part:
                    key, _, val = part.strip().partition("=")
                    jar[key] = val
            return jar
    return {}


def _is_open(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in OPEN_PREFIXES)


def _evaluate(scope) -> tuple[bool, str | None]:
    """Returns (trusted, extra_set_cookie_header).

    extra_set_cookie_header is only ever set on the device-token path: a
    rotated token (grant) or a dead one (revoked/reuse) both need the
    client's cookie updated, whether or not the request itself ends up
    trusted."""
    if (_really_local(scope) and _bind_is_loopback_only()
            and not auth.locked() and not auth.tunnel_mode()):
        return True, None

    session = auth.read_session(_cookies(scope))
    if session is not None:
        # A session issued via device enrollment carries that device's id
        # (see auth_routes.py's /auth/login-internet) precisely so this
        # check can happen: the session cookie alone must not outlive the
        # device it came from. Older sessions issued before this existed
        # have no device_id — they fall through to the device-cookie check
        # below instead, which enforces the same thing directly.
        if session.get("via") == "bootstrap":
            pass        # can only choose the first password (auth_routes.py)
        elif session.get("via") == "device":
            rec = devices.get(session.get("device_id", ""))
            if rec is not None and not rec.get("revoked"):
                return True, None
        else:
            return True, None

    dev = devices.read_cookie(_cookies(scope))
    if dev is not None:
        kind, new_token = devices.try_grant(dev["device_id"], dev["token"])
        secure = scope.get("scheme") == "https"
        if kind == "ok":
            cookie = (devices.set_cookie_header(dev["device_id"], new_token, secure)
                      if new_token else None)
            return True, cookie
        return False, devices.clear_cookie_header()

    return False, None


class _RotatingSend:
    """Wraps ASGI `send` to attach one extra Set-Cookie header to whichever
    message actually opens the response — http.response.start for HTTP,
    websocket.accept for websockets. Needed because gate.py sits in front of
    routes that build their own responses; this is the only place that can
    still add a header to them without every route knowing about devices.py."""

    def __init__(self, send, cookie_header: str):
        self._send = send
        self._cookie_header = cookie_header
        self._attached = False

    async def __call__(self, message) -> None:
        if not self._attached and message["type"] in (
                "http.response.start", "websocket.accept"):
            self._attached = True
            headers = list(message.get("headers", []))
            headers.append((b"set-cookie", self._cookie_header.encode("latin-1")))
            message = {**message, "headers": headers}
        await self._send(message)


class GateMiddleware:
    """Raw ASGI middleware — BaseHTTPMiddleware cannot see websockets."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)          # lifespan, etc.
            return

        path = scope.get("path", "")
        if path == assist.ENDPOINT:
            # Reached only from a shell on this machine, never from a browser:
            # loopback AND the token derived for that shell, no cookie.
            if (scope["type"] == "http" and _client_is_loopback(scope)
                    and assist.verify_headers(scope.get("headers", ()))):
                await self.app(scope, receive, send)
            else:
                await JSONResponse({"detail": "Forbidden."}, status_code=403)(scope, receive, send)
            return

        if _needs_origin_check(scope) and not _same_origin(scope):
            # Before anything else, the login routes included: a foreign page
            # must not be able to try passwords or open a shell through us.
            if scope["type"] == "websocket":
                await receive()                            # consume connect
                await send({"type": "websocket.close", "code": 4403,
                            "reason": "this page's address is not this Shellside's own "
                                      "(see allowed_origins in the README)"})
            else:
                await JSONResponse({"detail": "Cross-site request refused."},
                                   status_code=403)(scope, receive, send)
            return

        if _is_open(path):
            await self.app(scope, receive, send)
            return

        trusted, cookie_header = _evaluate(scope)
        if cookie_header:
            send = _RotatingSend(send, cookie_header)

        if trusted:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await receive()                                # consume connect
            await send({"type": "websocket.close", "code": 4401})
            return

        # HTTP, not open, not trusted.
        if path.startswith("/api/"):
            response = JSONResponse({"detail": "Not authenticated."}, status_code=401)
        else:
            response = RedirectResponse(f"/auth/login?next={path}", status_code=302)
        await response(scope, receive, send)
