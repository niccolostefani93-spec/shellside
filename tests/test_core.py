"""Regression tests for the behaviours that are easy to break silently."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.pty_session import PtySession
from server.templating import render
from server.fsview import breadcrumb


def drain(session, seconds=0.4):
    end = time.time() + seconds
    out = b""
    while time.time() < end:
        try:
            out += os.read(session.fd, 65536)
        except BlockingIOError:
            time.sleep(0.02)
        except OSError:
            break
    return out


def test_cwd_survives_every_form_of_cd():
    """The cases a stdin parser gets wrong."""
    s = PtySession(shell="/bin/bash", cwd="/tmp")
    drain(s, 0.6)
    for command, expected in [
        (b"cd /tmp\n", "/tmp"),
        (b"mkdir -p '/tmp/a b' && cd '/tmp/a b'\n", "/tmp/a b"),
        (b"cd -\n", "/tmp"),
        (b"pushd /var/log >/dev/null\n", "/var/log"),
        (b"T=/etc; cd $T\n", "/etc"),
        (b"g(){ cd /usr/share; }; g\n", "/usr/share"),
    ]:
        s.write(command)
        drain(s, 0.4)
        assert s.cwd() == expected, f"{command!r} -> {s.cwd()} (want {expected})"
    s.close()


def test_inject_blocked_while_a_program_runs():
    s = PtySession(shell="/bin/bash", cwd="/tmp")
    drain(s, 0.6)
    assert s.is_idle()
    s.write(b"sleep 2\n")
    time.sleep(0.5)
    assert not s.is_idle()
    ok, reason = s.inject("rm -rf /tmp/nope", execute=True)
    assert not ok and "busy" in reason
    time.sleep(2)
    drain(s, 0.3)
    ok, _ = s.inject("echo MARKER", execute=True)
    assert ok
    assert b"MARKER" in drain(s, 0.6)
    s.close()


def test_hostile_filenames_are_quoted():
    out, missing = render("rm -rf {file_path}", {"file_path": "/tmp/a b; rm -rf ~"})
    assert out == "rm -rf '/tmp/a b; rm -rf ~'"
    assert not missing


def test_missing_placeholders_are_reported():
    out, missing = render("mv {file_path} {target_path}", {"file_path": "/tmp/x"})
    assert missing == ["target_path"]
    assert "{target_path}" in out


def test_breadcrumb_segments_are_absolute():
    assert breadcrumb("/etc/ssl/certs") == [
        {"label": "/", "path": "/"},
        {"label": "etc", "path": "/etc"},
        {"label": "ssl", "path": "/etc/ssl"},
        {"label": "certs", "path": "/etc/ssl/certs"},
    ]


def test_update_rules():
    """The refusals that protect the project from a malformed update."""
    import json, zipfile, tempfile, shutil
    from pathlib import Path as P
    from server import update_plan as up

    base = P(tempfile.mkdtemp())
    for root in up.MERGE_ROOTS:
        (base / root).mkdir(parents=True, exist_ok=True)
    (base / "app" / "doomed.json").write_text("{}")

    def zip_of(entries):
        path = base / "u.zip"
        with zipfile.ZipFile(path, "w") as z:
            for name, content in entries.items():
                z.writestr(name, content)
        return path.read_bytes()

    # path traversal is dropped at staging
    raw = zip_of({"../../evil.txt": "no", "server/ok.py": "OK = 1\n"})
    plan = up.build_plan(base, up.stage_zip(base, raw, "u.zip"))
    assert all(".." not in c["to"] for c in plan["copy"])
    up.clear_pending(base)

    # a delete cannot remove an entire root, nor escape the perimeter
    raw = zip_of({"server/ok.py": "OK = 1\n",
                  "delete.json": json.dumps({"delete": ["server", "/etc/passwd",
                                                        "app/doomed.json"]})})
    plan = up.build_plan(base, up.stage_zip(base, raw, "u.zip"))
    assert plan["delete"] == ["app/doomed.json"], plan["delete"]
    assert len(plan["warnings"]) >= 2
    up.clear_pending(base)

    # a .py in MERGE_FILES that does not compile is refused outright
    raw = zip_of({"run.py": "def broken(:\n", "server/ok.py": "OK = 1\n"})
    try:
        up.build_plan(base, up.stage_zip(base, raw, "u.zip"))
        raise AssertionError("a broken run.py was accepted")
    except up.UpdateError as exc:
        assert "compile" in str(exc)
    up.clear_pending(base)

    # arming requires the token from the preview the user actually saw
    raw = zip_of({"server/ok.py": "OK = 1\n"})
    plan = up.build_plan(base, up.stage_zip(base, raw, "u.zip"))
    up.write_plan(base, plan)
    try:
        up.arm(base, "wrong-token")
        raise AssertionError("armed with a stale token")
    except up.UpdateError:
        pass
    up.arm(base, plan["token"])
    assert up.pending_status(base)["armed"]
    shutil.rmtree(base, ignore_errors=True)


def test_auth_password_hash_and_verify():
    import shutil, tempfile
    from pathlib import Path as P
    from server import auth

    tmp = P(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / ".auth"
    auth._key_cache = None
    try:
        assert not auth.password_is_set()
        auth.set_password("correct-horse-battery")
        assert auth.password_is_set()
        assert auth.verify_password("correct-horse-battery")
        assert not auth.verify_password("wrong")
        auth.clear_password()
        assert not auth.password_is_set()
    finally:
        auth.AUTH_DIR = original
        auth._key_cache = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_auth_session_roundtrip_and_epoch_invalidation():
    import shutil, tempfile, time
    from pathlib import Path as P
    from server import auth

    tmp = P(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / ".auth"
    auth._key_cache = None
    try:
        token = auth.sign({"via": "password", "iat": time.time(),
                           "ep": auth.session_epoch()})
        payload = auth.read_session({auth.COOKIE_NAME: token})
        assert payload is not None and payload["via"] == "password"

        # tampering breaks the signature. Not via the last character: of a
        # 32-byte signature's 43 base64 chars, the last one carries 2 unused
        # bits, so swapping it for a neighbour (a <-> b) can decode to the
        # very same bytes - which is what made this test flaky.
        body, sig = token.split(".", 1)
        broken = f"{body}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
        assert auth.read_session({auth.COOKIE_NAME: broken}) is None
        forged = auth._b64(b'{"via":"password","iat":%f,"ep":%d}'
                           % (time.time(), auth.session_epoch() + 99))
        assert auth.read_session({auth.COOKIE_NAME: f"{forged}.{sig}"}) is None

        # a non-canonical spelling of the same signature is refused too
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alias = alphabet[alphabet.index(sig[-1]) ^ 1]      # flips an unused bit
        assert auth._unb64(sig[:-1] + alias) == auth._unb64(sig)
        assert auth.read_session({auth.COOKIE_NAME: f"{body}.{sig[:-1]}{alias}"}) is None

        # bumping the epoch (password change / sign-out-everywhere) kills it
        auth.bump_session_epoch()
        assert auth.read_session({auth.COOKIE_NAME: token}) is None

        # a "local" session is exempt from the epoch, on purpose
        local_token = auth.sign({"via": "local", "iat": time.time(),
                                 "ep": auth.session_epoch() - 1})
        assert auth.read_session({auth.COOKIE_NAME: local_token}) is not None
    finally:
        auth.AUTH_DIR = original
        auth._key_cache = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_auth_rate_limiting():
    import shutil, tempfile
    from pathlib import Path as P
    from server import auth

    tmp = P(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / ".auth"
    auth._key_cache = None
    auth._ip_fails.clear()
    auth._global_fails = 0
    try:
        auth.set_setting("block_ip_after", 3)
        ip = "198.51.100.7"
        assert not auth.ip_blocked(ip)
        for _ in range(3):
            auth.register_failure(ip)
        assert auth.ip_blocked(ip)
        auth.register_success(ip)
        assert not auth.ip_blocked(ip)
    finally:
        auth.AUTH_DIR = original
        auth._key_cache = None
        auth._ip_fails.clear()
        auth._global_fails = 0
        shutil.rmtree(tmp, ignore_errors=True)


def test_auth_local_recovery_token_not_single_use():
    """Deliberately not consumed on use - see server/auth.py docstring: a
    single-use token means one lost cookie locks you out until reboot."""
    import shutil, tempfile
    from pathlib import Path as P
    from server import auth

    tmp = P(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / ".auth"
    auth._key_cache = None
    try:
        token = auth.new_local_token()
        assert auth.consume_local_token(token)
        assert auth.consume_local_token(token)          # still works
        assert not auth.consume_local_token("wrong-token")
        assert not auth.consume_local_token("")
    finally:
        auth.AUTH_DIR = original
        auth._key_cache = None
        shutil.rmtree(tmp, ignore_errors=True)


def test_gate_open_prefixes_and_loopback_rule():
    from server import gate

    assert gate._is_open("/auth/login")
    assert gate._is_open("/static/css/app.css")
    assert not gate._is_open("/api/bootstrap")
    assert not gate._is_open("/")

    # Both modules read SHELLSIDE_BIND_HOST on every call, not at import, so
    # setting the variable here is enough. It must also be restored: a test
    # run from a shell inside Shellside inherits the live server's value.
    import shutil, tempfile
    from pathlib import Path as P
    from server import auth, auth_routes

    def scope(*extra_headers):
        headers = [(b"host", b"127.0.0.1:8022"), *extra_headers]
        return {"type": "http", "method": "GET", "path": "/",
                "client": ("127.0.0.1", 50000), "headers": headers}

    tmp = P(tempfile.mkdtemp())
    original = auth.AUTH_DIR
    auth.AUTH_DIR = tmp / ".auth"          # locked / tunnel_mode read from here
    auth._key_cache = None
    old = os.environ.pop("SHELLSIDE_BIND_HOST", None)
    try:
        # unset: run.py's default is 127.0.0.1
        assert gate._bind_is_loopback_only() and auth_routes._bind_is_loopback_only()
        assert gate._evaluate(scope()) == (True, None)

        for host in ("127.0.0.1", "::1"):
            os.environ["SHELLSIDE_BIND_HOST"] = host
            assert gate._bind_is_loopback_only()
            assert auth_routes._bind_is_loopback_only()
            assert gate._evaluate(scope()) == (True, None)

        # bound to every interface: loopback alone no longer proves anything
        os.environ["SHELLSIDE_BIND_HOST"] = "0.0.0.0"
        assert not gate._bind_is_loopback_only()
        assert not auth_routes._bind_is_loopback_only()
        assert gate._evaluate(scope()) == (False, None)

        # bound to loopback, but relayed by a proxy/tunnel on this machine
        os.environ["SHELLSIDE_BIND_HOST"] = "127.0.0.1"
        assert {b"cf-connecting-ip", b"x-forwarded-for", b"forwarded"} <= set(gate.PROXY_HEADERS)
        for name in gate.PROXY_HEADERS:
            assert not gate._really_local(scope((name, b"203.0.113.9")))
            assert gate._evaluate(scope((name, b"203.0.113.9"))) == (False, None)

        # DNS rebinding: loopback client, but the Host is someone else's name
        rebound = scope()
        rebound["headers"] = [(b"host", b"evil.example:8022")]
        assert gate._evaluate(rebound) == (False, None)

        # a deliberate sign-out, or tunnel mode, suspends the rule too
        auth.set_locked(True)
        assert gate._evaluate(scope()) == (False, None)
        auth.set_locked(False)
        auth.set_tunnel_mode(True)
        assert gate._evaluate(scope()) == (False, None)
    finally:
        if old is not None:
            os.environ["SHELLSIDE_BIND_HOST"] = old
        else:
            os.environ.pop("SHELLSIDE_BIND_HOST", None)
        auth.AUTH_DIR = original
        auth._key_cache = None
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"  pass  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'all green' if not failures else f'{failures} failing'}")
    sys.exit(1 if failures else 0)


# ------------------------------------------------------------- DATA_HOME

def _fake_checkout(tmp):
    """A code directory with the shipped defaults datahome.py seeds from."""
    import json
    root = tmp / "code"
    (root / "app" / "commands").mkdir(parents=True)
    (root / "app" / "commands" / "demo.json").write_text(
        json.dumps({"name": "Demo", "command": "df -h"}))
    (root / "app" / "favorites").mkdir()
    (root / "app" / "favorites" / "paths.json").write_text("[]")
    (root / "app" / "data").mkdir()
    (root / "app" / "data" / "service_config.json").write_text("{}")
    (root / "services" / "llm").mkdir(parents=True)
    (root / "services" / "llm" / "active.json").write_text('{"active": "echo"}')
    return root


def test_run_py_and_server_agree_on_data_home():
    """run.py keeps its own copy of the resolution; the two must not drift."""
    import importlib.util
    from server import datahome

    spec = importlib.util.spec_from_file_location(
        "shellside_run_under_test",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py"))
    run = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True       # no __pycache__/ left in the code dir
    try:
        spec.loader.exec_module(run)
    finally:
        sys.dont_write_bytecode = False

    saved = {k: os.environ.get(k) for k in ("SHELLSIDE_HOME", "XDG_CONFIG_HOME")}
    try:
        for home, xdg in [("/tmp/sh-a", None), ("~/sh-b", "/x"), ("", "/tmp/xdg"),
                          (None, "relative/ignored"), (None, None)]:
            for key, value in (("SHELLSIDE_HOME", home), ("XDG_CONFIG_HOME", xdg)):
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            assert run._data_home() == datahome.resolve_data_home(), (home, xdg)
            assert run._data_home().is_absolute()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_fresh_install_is_seeded_from_shipped_defaults():
    import json, shutil, tempfile
    from pathlib import Path as P
    from server import datahome

    tmp = P(tempfile.mkdtemp())
    try:
        root = _fake_checkout(tmp)
        home = tmp / "home"
        datahome.prepare(root, home)
        assert (home / "commands" / "demo.json").is_file()
        assert json.loads((home / "favorites" / "paths.json").read_text()) == []
        assert (home / "data" / "service_config.json").is_file()
        # Not seeded: settings come from DEFAULT_SETTINGS; auth is made on use;
        # a clean clone is not "legacy state", so no active-service pin either.
        assert not (home / "settings.json").exists()
        assert not (home / "auth").exists()
        assert not (home / "data" / "service_active.json").exists()
        assert (home.stat().st_mode & 0o777) == 0o700
        # Nothing was written into the code directory.
        assert sorted(p.name for p in root.iterdir()) == ["app", "services"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_legacy_state_is_copied_once_and_never_moved():
    import json, shutil, tempfile
    from pathlib import Path as P
    from server import datahome

    tmp = P(tempfile.mkdtemp())
    try:
        root = _fake_checkout(tmp)
        (root / "services" / "llm" / "active.json").write_text('{"active": "ollama"}')
        auth_dir = root / ".auth"
        auth_dir.mkdir(mode=0o700)
        (auth_dir / "secret_key").write_text("ab" * 32)
        os.chmod(auth_dir / "secret_key", 0o600)
        (auth_dir / "auth.json").write_text('{"password_hash": "x"}')
        (root / "app" / "settings.json").write_text('{"bind_host": "0.0.0.0"}')
        (root / "app" / "commands" / "mine.json").write_text(
            json.dumps({"name": "Mine", "command": "true"}))
        mine = root / "app" / "context_menu" / "file_entry" / "my_action"
        mine.mkdir(parents=True)
        (mine / "manifest.json").write_text('{"name": "Mine", "user": true}')
        (root / "app" / "context_menu" / "overrides.json").write_text(
            '{"file_entry/move": {"execute": true}}')

        home = tmp / "home"
        (home / "run").mkdir(parents=True)     # run.py's pid file may come first
        datahome.prepare(root, home)

        assert (home / "auth" / "secret_key").read_text() == "ab" * 32
        assert ((home / "auth").stat().st_mode & 0o777) == 0o700
        assert ((home / "auth" / "secret_key").stat().st_mode & 0o777) == 0o600
        assert json.loads((home / "settings.json").read_text())["bind_host"] == "0.0.0.0"
        assert (home / "commands" / "mine.json").is_file()
        assert (home / "commands" / "demo.json").is_file()
        assert (home / "context_menu" / "file_entry" / "my_action" / "manifest.json").is_file()
        assert (home / "context_menu" / "overrides.json").is_file()
        assert json.loads((home / "data" / "service_active.json").read_text()) == {"llm": "ollama"}
        # Copied, not moved.
        assert (auth_dir / "secret_key").is_file()
        assert (root / "app" / "settings.json").is_file()
        assert not list(tmp.glob(".home.migrating-*"))

        # Idempotent: a second start changes nothing, even if the old copy did.
        (home / "settings.json").write_text('{"bind_host": "127.0.0.1"}')
        (root / "app" / "settings.json").write_text('{"bind_host": "10.0.0.1"}')
        datahome.prepare(root, home)
        assert json.loads((home / "settings.json").read_text())["bind_host"] == "127.0.0.1"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_existing_data_home_is_never_overwritten():
    import json, shutil, tempfile
    from pathlib import Path as P
    from server import datahome

    tmp = P(tempfile.mkdtemp())
    try:
        root = _fake_checkout(tmp)
        (root / ".auth").mkdir()
        (root / ".auth" / "secret_key").write_text("legacy")
        (root / "app" / "settings.json").write_text('{"font_size": 99}')

        home = tmp / "home"
        (home / "commands").mkdir(parents=True)
        (home / "commands" / "kept.json").write_text('{"name": "Kept", "command": "x"}')
        (home / "settings.json").write_text('{"font_size": 12}')
        (home / "favorites").mkdir()
        (home / "favorites" / "paths.json").write_text('[{"path": "/srv", "label": "srv"}]')

        datahome.prepare(root, home)
        assert json.loads((home / "settings.json").read_text()) == {"font_size": 12}
        assert not (home / "auth").exists()
        # A commands folder that exists is yours: a deleted demo stays deleted.
        assert sorted(p.name for p in (home / "commands").iterdir()) == ["kept.json"]
        assert json.loads((home / "favorites" / "paths.json").read_text())[0]["path"] == "/srv"
        # A missing per-file default is still filled in.
        assert (home / "data" / "service_config.json").is_file()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_editors_write_to_data_home_not_the_code_directory():
    """Every interface edit lands in DATA_HOME; the shipped layer is untouched."""
    import json
    from server import (command_editor, components, config, menu_editor,
                        script_editor, services)

    def snapshot():
        return {str(p): p.stat().st_mtime_ns
                for top in (config.APP_DIR, config.SERVICES_DIR)
                for p in top.rglob("*") if "__pycache__" not in p.parts}

    before = snapshot()
    home = config.DATA_HOME
    assert str(home).startswith(os.environ["SHELLSIDE_HOME"])

    cid = command_editor.create({"name": "Test cmd", "command": "true"})
    assert (home / "commands" / f"{cid}.json").is_file()
    command_editor.remove(cid)

    action = menu_editor.create({"surface": "file_entry", "name": "Test action",
                                 "command": "true"})
    assert menu_editor.is_user_owned("file_entry", action)
    assert (home / "context_menu" / "file_entry" / action / "manifest.json").is_file()
    menu_editor.patch("file_entry", "rename", {"execute": True})
    assert "file_entry/rename" in menu_editor.load_overrides()
    tree = components.discover(force=True)
    ids = [item["id"] for item in tree["context_menus"]["file_entry"]]
    assert action in ids and "rename" in ids
    menu_editor.remove("file_entry", action)
    menu_editor.reset("file_entry", "rename")

    script_editor.patch("settings_panel", {"name": "Prefs"})
    tree = components.discover(force=True)
    assert [s["name"] for s in tree["scripts"] if s["id"] == "settings_panel"] == ["Prefs"]
    script_editor.remove("settings_panel")
    tree = components.discover(force=True)
    assert "settings_panel" not in [s["id"] for s in tree["scripts"]]
    (home / "scripts" / "overrides.json").unlink()

    services.set_active("llm", "ollama")
    assert services.active_id("llm") == "ollama"
    assert json.loads((home / "data" / "service_active.json").read_text()) == {"llm": "ollama"}
    (home / "data" / "service_active.json").unlink()
    assert services.active_id("llm") == "echo"        # the shipped default again

    config.save_settings({"font_size": 15})
    assert json.loads((home / "settings.json").read_text())["font_size"] == 15

    assert snapshot() == before


def test_file_icons_are_validated():
    import pytest
    from server.config import DEFAULT_FILE_ICONS, SettingsError, normalize_file_icons

    clean = normalize_file_icons(DEFAULT_FILE_ICONS)
    assert clean["folder"] == DEFAULT_FILE_ICONS["folder"]
    assert len(clean["groups"]) == len(DEFAULT_FILE_ICONS["groups"])

    out = normalize_file_icons({"folder": "", "groups": [
        {"name": "Mine", "icon": "X" * 20, "extensions": ["JPG", "png", ""]}]})
    assert out["folder"] == DEFAULT_FILE_ICONS["folder"]          # empty -> default
    assert out["groups"][0]["extensions"] == [".jpg", ".png"]
    assert len(out["groups"][0]["icon"]) <= 8
    with pytest.raises(SettingsError):
        normalize_file_icons({"groups": [{"name": "", "icon": "x", "extensions": [".a"]}]})


def test_each_data_home_gets_its_own_tmux_server(monkeypatch, tmp_path):
    from server import pty_session

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    default = pty_session.datahome.default_data_home()
    assert pty_session.tmux_socket_name(default) == "shellside"
    other = pty_session.tmux_socket_name(tmp_path / "a")
    assert other.startswith("shellside-") and other != "shellside"
    assert other != pty_session.tmux_socket_name(tmp_path / "b")
    # The test run's own DATA_HOME never shares the real install's shells.
    assert pty_session.TMUX_SOCKET != "shellside"


def test_assistant_sees_only_the_previous_command():
    from server.assist import last_command_block

    screen = [
        "nik@pc:~$ voglio cambiare cartella come faccio?",
        "bash: voglio: command not found",
        "  ▸ cd Documenti",
        "nik@pc:~$ which server",
        "nik@pc:~$ ls /nonexist",
        "ls: cannot access '/nonexist': No such file or directory",
        "nik@pc:~$ perché non funziona?",
    ]
    assert last_command_block(screen, "ls /nonexist") == screen[4:6]
    # Earlier questions and answers never reach the model.
    assert last_command_block(screen[:5], "which server") == ["nik@pc:~$ which server"]
    # Not on screen (cleared, or from an earlier session's history): nothing.
    assert last_command_block(screen, "make install") == []
    assert last_command_block(screen, "") == []

    long_output = ["nik@pc:~$ seq 100", *map(str, range(1, 101)), "nik@pc:~$ e ora?"]
    block = last_command_block(long_output, "seq 100")
    assert block[0] == "nik@pc:~$ seq 100" and block[-1] == "100"
    assert len(block) == 32          # prompt line, "left out" note, last 30 lines
