"""The setup (bootstrap) password and the never-expose-without-a-credential
rule. These are the invariants listed in the release plan:

  1. no non-loopback bind unless a real credential (or a live setup password,
     or a deliberate lan_open) exists, enforced at startup;
  2. removing every credential reverts to loopback only;
  3. the setup password is refused once a real password exists, through a
     proxy or tunnel, after it expires, and after it was used once;
  4. setup never overwrites or weakens an existing real credential.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import auth  # noqa: E402

LAN_CLIENT = ("192.168.1.50", 50000)


@pytest.fixture
def auth_dir():
    tmp = Path(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / "auth"
    auth._key_cache = None
    auth._ip_fails.clear()
    auth._global_fails = 0
    auth._last_attempt = 0.0
    try:
        yield auth.AUTH_DIR
    finally:
        auth.AUTH_DIR = original
        auth._key_cache = None
        auth._ip_fails.clear()
        auth._global_fails = 0
        auth._last_attempt = 0.0
        shutil.rmtree(tmp, ignore_errors=True)


def _load_run_py():
    spec = importlib.util.spec_from_file_location("shellwalk_run_bootstrap", ROOT / "run.py")
    run = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(run)
    finally:
        sys.dont_write_bytecode = False
    return run


def test_bootstrap_password_shape_and_lifetime(auth_dir):
    password = auth.issue_bootstrap_password()
    assert len(password) == auth.BOOTSTRAP_LENGTH
    assert set(password) <= set(auth.BOOTSTRAP_ALPHABET)
    assert not set(password) & set("0O1lI")
    assert password not in (auth_dir / "auth.json").read_text()     # hash only
    assert auth.bootstrap_active() and auth.verify_bootstrap_password(password)
    assert not auth.verify_bootstrap_password(password.swapcase())

    # expired: refused even though it is correct
    auth.set_setting("bootstrap_expires_at", time.time() - 1)
    assert not auth.bootstrap_active()
    assert not auth.verify_bootstrap_password(password)


def test_bootstrap_never_weakens_a_real_password(auth_dir):
    password = auth.issue_bootstrap_password()
    auth.set_password("a-real-password")
    assert not auth.bootstrap_active()
    assert not auth.verify_bootstrap_password(password)
    assert auth.issue_bootstrap_password() is None       # setup re-run
    assert auth.verify_password("a-real-password")


def test_exposure_rule_and_reset(auth_dir, monkeypatch):
    run = _load_run_py()
    monkeypatch.setattr(run, "AUTH_FILE", auth_dir / "auth.json")

    def both():
        assert run.lan_exposure_allowed() == auth.lan_exposure_allowed()
        return auth.lan_exposure_allowed()

    assert not both()
    assert run.effective_host("0.0.0.0") == "127.0.0.1"
    assert run.effective_host("127.0.0.1") == "127.0.0.1"

    auth.issue_bootstrap_password()
    assert both() and run.effective_host("0.0.0.0") == "0.0.0.0"
    auth.set_setting("bootstrap_expires_at", time.time() - 1)
    assert not both()

    auth.set_password("a-real-password")
    assert both()
    auth.reset_access()
    assert not both() and not auth.password_is_set() and not auth.lan_open()

    auth.set_setting("lan_open", True)                 # the deliberate choice
    assert both()


def _client(app, client=LAN_CLIENT):
    from starlette.testclient import TestClient
    return TestClient(app, base_url="http://192.168.1.10:8022", client=client)


def test_bootstrap_login_flow(auth_dir, monkeypatch):
    monkeypatch.setenv("SHELLWALK_BIND_HOST", "0.0.0.0")
    monkeypatch.setattr(auth, "DEFAULTS", {**auth.DEFAULTS, "throttle_seconds": 0})
    from server.main import app
    from server import config
    monkeypatch.setattr(config, "save_settings", lambda patch: patch)

    password = auth.issue_bootstrap_password()
    web = _client(app)
    origin = {"Origin": "http://192.168.1.10:8022"}

    # wrong password: plain failure
    r = web.post("/auth/login", json={"password": "nope"}, headers=origin)
    assert r.status_code == 401

    # through a proxy: refused even with the right password
    r = web.post("/auth/login", json={"password": password},
                 headers={**origin, "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401 and auth.bootstrap_active()

    # from loopback (a tunnel looks like this): refused
    r = _client(app, ("127.0.0.1", 50000)).post(
        "/auth/login", json={"password": password}, headers=origin)
    assert r.status_code == 403

    r = web.post("/auth/login", json={"password": password}, headers=origin)
    assert r.status_code == 200 and r.json()["must_set_password"]
    assert not auth.bootstrap_active()                   # consumed

    # the bootstrap session opens nothing but the first-password step
    assert web.get("/api/access/status").status_code == 401
    assert web.get("/", follow_redirects=False).status_code == 302

    # used once: a second login with it fails
    other = _client(app, ("192.168.1.51", 50000))
    assert other.post("/auth/login", json={"password": password},
                      headers=origin).status_code == 403

    r = web.post("/auth/first-password", json={"password": "short"}, headers=origin)
    assert r.status_code == 400
    r = web.post("/auth/first-password", json={"password": "my-real-password"},
                 headers=origin)
    assert r.status_code == 200 and auth.verify_password("my-real-password")
    assert web.get("/api/access/status").status_code == 200

    # cannot be used again to replace the real password
    r = web.post("/auth/first-password", json={"password": "attacker-pass"},
                 headers=origin)
    assert r.status_code == 403 and auth.verify_password("my-real-password")


def test_first_password_needs_a_bootstrap_session(auth_dir):
    from server.main import app
    web = _client(app)
    r = web.post("/auth/first-password", json={"password": "my-real-password"},
                 headers={"Origin": "http://192.168.1.10:8022"})
    assert r.status_code == 403 and not auth.password_is_set()


def test_setup_tunnel_question_sets_tunnel_mode(auth_dir, monkeypatch):
    from server import wizard
    monkeypatch.setattr(wizard, "systemd_present", lambda: False)
    monkeypatch.setattr(wizard, "save_settings", lambda patch: patch)
    monkeypatch.setattr(wizard, "port_in_use", lambda port: False)
    base = ["--local", "--port", "8095", "--no-autostart", "--yes"]
    assert wizard.setup([*base, "--tunnel"]) == 0 and auth.tunnel_mode()
    assert wizard.setup([*base, "--no-tunnel"]) == 0 and not auth.tunnel_mode()

    # an active internet password keeps tunnel mode on whatever the answer
    auth.set_internet_password("internet-pass", slots=1, days=1)
    assert auth.tunnel_mode()
    wizard.setup([*base, "--no-tunnel"])
    assert auth.tunnel_mode()


def test_local_token_not_printed_without_a_terminal(auth_dir, capsys):
    import asyncio
    from server import main
    asyncio.run(main._rotate_local_token())          # capsys: not a TTY
    out = capsys.readouterr().out
    assert auth.local_token() and auth.local_token() not in out
    assert "./shellwalk link" in out
