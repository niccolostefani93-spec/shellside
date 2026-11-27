#!/usr/bin/env python3
"""Shellwalk — launcher.

    ./shellwalk                    # start it (settings from ./shellwalk setup)
    ./shellwalk setup              # guided setup: LAN or not, port, autostart
    ./shellwalk reset-auth         # forgot the password: back to unconfigured
    ./shellwalk link               # sign-in link for a browser on this machine
    python3 run.py --host 0.0.0.0  # the same, by hand (needs a LAN password)

This file checks the setup, applies an armed update, and then runs the app in
this same process. There is no second process waiting around: when the app
needs a restart (an applied update, a new bind address) it re-executes this
file in place (os.execv, same PID), so every restart passes through here again.

That is what makes an update safe to apply: the merge happens *here*, at the
top of main(), before server/ is imported, with the old code already gone
from memory.

If the app overwrote its own files while running, the process would be left with
old code in memory and new code on disk — and on a headless box that is not
something you can fix remotely.

    app asks for a restart
              |
              v
    os.execv(run.py)  (old code gone from memory)
        - copies the files in the plan
        - performs the deletions
        - deletes _pending_update/
        - starts the app again

A crash is not retried here: under systemd, Restart=on-failure does that.

The constants below are duplicated from server/update_plan.py on purpose. This
file must be able to apply an update even when that update has broken server/.
They are a contract on disk, not shared code.
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _data_home() -> Path:
    """Where user state lives. A copy of server/datahome.resolve_data_home().

    Duplicated for the same reason as the update constants below: the pid file
    and the saved bind address must still be found when server/ cannot be
    imported. tests/test_core.py checks that the two copies agree.
    """
    explicit = os.environ.get("SHELLWALK_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "shellwalk"


DATA_HOME = _data_home()


def prepare_data_home() -> None:
    """Copy pre-DATA_HOME state out of the code directory, and seed defaults.

    Runs before this process writes its own pid file into DATA_HOME or reads
    the bind address from it, so a first start after upgrading still finds
    the bind address and credentials of the old layout. server/datahome.py is
    stdlib-only and imports nothing else from server/, so this works before
    preflight(); if it cannot be imported at all, the app does the same thing
    itself when it starts (server/config.py), and the only cost is that this
    one start reads defaults instead of the migrated settings.
    """
    try:
        from server import datahome
        datahome.prepare(ROOT, DATA_HOME)
    except Exception as exc:                    # must never stop the start
        log(f"could not prepare {DATA_HOME}: {exc}", RED)


PENDING_DIRNAME = "_pending_update"
PAYLOAD_DIRNAME = "payload"
MANIFEST_FILENAME = "manifest.json"
MERGE_ROOTS = ("server", "web", "app", "services", "deploy")
MERGE_FILES = ("README.md", "run.py", "requirements.txt")

BANNER = "\033[38;5;179m"
DIM = "\033[38;5;244m"
RED = "\033[38;5;167m"
GREEN = "\033[38;5;108m"
OFF = "\033[0m"


def log(message: str, colour: str = DIM) -> None:
    print(f"{colour}  [shellwalk] {message}{OFF}", flush=True)


# --- Applying a pending update -----------------------------------------------

def _safe_target(relative: str) -> Path | None:
    """Resolve a plan path and refuse anything that escapes the project."""
    candidate = (ROOT / relative).resolve()
    try:
        candidate.relative_to(ROOT)
    except ValueError:
        return None
    parts = Path(relative).parts
    if not parts:
        return None
    if parts[0] in MERGE_ROOTS:
        return candidate
    if len(parts) == 1 and parts[0] in MERGE_FILES:
        return candidate
    return None


def _self_update(source: Path, destination: Path) -> bool:
    """Overwrite run.py, with the three defences described in the README."""
    if getattr(sys, "frozen", False):
        log("frozen build: skipping run.py, rebuild the executable instead", RED)
        return False
    try:
        py_compile.compile(str(source), cfile=str(source) + ".check",
                           doraise=True, quiet=1)
    except py_compile.PyCompileError as exc:
        log(f"incoming run.py does not compile, keeping the current one: {exc}", RED)
        return False
    finally:
        Path(str(source) + ".check").unlink(missing_ok=True)

    backup = destination.with_name("run.py.bak")
    try:
        shutil.copy2(destination, backup)
        log(f"previous run.py kept at {backup.name}")
    except OSError as exc:
        log(f"could not write {backup.name}: {exc}", RED)
    shutil.copy2(source, destination)
    return True


def apply_pending_update() -> bool:
    """Apply an armed update. Returns True if run.py replaced itself."""
    pending = ROOT / PENDING_DIRNAME
    manifest_file = pending / MANIFEST_FILENAME
    if not manifest_file.is_file():
        return False

    payload = pending / PAYLOAD_DIRNAME
    replaced_self = False
    try:
        plan = json.loads(manifest_file.read_text())
        copies = plan.get("copy", [])
        deletes = plan.get("delete", [])
        log(f"applying update from {plan.get('source') or 'import'}: "
            f"{len(copies)} file(s), {len(deletes)} deletion(s)", BANNER)

        # Copies first, then deletions. Always this order.
        for entry in copies:
            source = payload / entry["from"]
            destination = _safe_target(entry["to"])
            if destination is None or not source.is_file():
                log(f"skipped {entry['to']}", RED)
                continue
            if destination.name == "run.py" and destination.parent == ROOT:
                replaced_self = _self_update(source, destination) or replaced_self
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        for relative in deletes:
            target = _safe_target(relative)
            if target is None or not target.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)

        log("update applied", GREEN)
    except Exception as exc:                      # must never stop the start
        log(f"update failed part way through: {exc}", RED)
    finally:
        # Always remove it. A manifest that survived an error would redo the
        # same merge on every start, forever.
        shutil.rmtree(pending, ignore_errors=True)

    return replaced_self


def relaunch_self(argv: list[str]) -> None:
    log("run.py updated itself, restarting", BANNER)
    os.execv(sys.executable, [sys.executable, str(ROOT / "run.py"), *argv])


def relaunch_into_venv(argv: list[str]) -> None:
    """Re-exec with the virtualenv's interpreter after setup just built one.

    We were started with whatever `python3` the person typed — almost never
    the venv's own, since the venv didn't exist yet. The packages setup.sh
    just installed are only importable from inside it, so this process, as
    it stands, still can't `import uvicorn`. Same fix as an applied update:
    replace this process rather than try to patch its already-loaded state.
    """
    venv_python = ROOT / ".venv" / "bin" / "python"
    log(f"restarting with {venv_python}", BANNER)
    os.execv(str(venv_python), [str(venv_python), str(ROOT / "run.py"), *argv])


# --- Our own previous instance, and only that ---------------------------------

# In DATA_HOME rather than $XDG_RUNTIME_DIR: that is only set for login
# sessions, so a systemd system unit or a cron start would need a second
# fallback anyway, and one location resolved one way is the thing run.py and
# the app can be kept in step on. The file records ROOT, so two code checkouts
# sharing one DATA_HOME do not mistake each other for themselves.
RUN_DIR = DATA_HOME / "run"
PIDFILE = RUN_DIR / "shellwalk.pid"
# Where earlier versions wrote it. Read, never written: the first start after
# upgrading may still find the old instance on the port.
_LEGACY_PIDFILES = (RUN_DIR / "supervisor.pid", ROOT / ".run" / "supervisor.pid")


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def record_self() -> None:
    """Note this process, which holds the port, for a later start to find.

    Not a lockfile in the exclusive-open sense — nothing here refuses to run
    because the file exists. It exists so that later, ensure_port_free() has
    something more trustworthy than "something is listening on 8022" to go
    on before it decides whether that something is safe to end.
    """
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(json.dumps({"pid": os.getpid(), "root": str(ROOT)}))
    except OSError:
        pass  # best-effort; ensure_port_free() degrades to "leave it alone"


def _runs_our_run_py(pid: int) -> bool:
    """Whether that process is a Python running this very run.py.

    Resolved against the process's own cwd, because `python3 run.py` from
    inside the checkout leaves no absolute path in the command line.
    """
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    cwd = _proc_cwd(pid)
    ours = str(ROOT / "run.py")
    for raw in args[1:3]:               # python [flags] run.py ...
        arg = raw.decode("utf-8", "replace")
        if os.path.basename(arg) == "run.py" and cwd:
            if os.path.normpath(os.path.join(cwd, arg)) == ours:
                return True
    return False


def _proc_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _stale_self_pid() -> int | None:
    """A PID from our own pidfile, but only if it still checks out as *us*.

    Today that process is run.py itself, whose argv contains "run.py" and
    this ROOT. Earlier versions ran the app as the child of a supervisor,
    an inline `python -c "import uvicorn; ..."` with neither, so for an
    instance left over from an upgrade its *cwd* is checked instead — only a
    process we spawned ever had cwd=ROOT and uvicorn in its argv. A recycled
    PID (Linux reuses them) matching neither shape is not good enough.
    """
    for pidfile in (PIDFILE, *_LEGACY_PIDFILES):
        try:
            data = json.loads(pidfile.read_text())
            pid = int(data["pid"])
            if data.get("root") != str(ROOT):
                continue
            break
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    else:
        return None
    if pid == os.getpid():
        return None

    cmdline = _read_cmdline(pid)
    if not cmdline:
        return None
    looks_like_us = _runs_our_run_py(pid)
    looks_like_old_child = "uvicorn" in cmdline and _proc_cwd(pid) == str(ROOT)
    if not (looks_like_us or looks_like_old_child):
        return None
    return pid


def _port_is_open(host: str, port: int) -> bool:
    import socket
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe_host, port)) == 0


def _find_port_owner(port: int) -> str:
    """Best-effort description of whatever holds the port, for the message.

    Reading another process's open file descriptors needs that process's own
    permissions, which we usually don't have — this fails silently far more
    often than it succeeds, and that is fine. Its only job is to make the
    refusal message specific when it can be, never to make the decision.
    """
    try:
        import subprocess as _sp
        out = _sp.run(["ss", "-ltnp", f"sport = :{port}"], capture_output=True,
                       text=True, timeout=2).stdout
        for line in out.splitlines():
            if f":{port} " in line or line.rstrip().endswith(f":{port}"):
                return line.strip()
    except Exception:
        pass
    return ""


def ensure_port_free(host: str, port: int) -> bool:
    """End our own stale instance if that's what's on the port. Nothing else.

    A port left open after an unclean exit — a killed terminal, a lost SSH
    session, a power blip — is the ordinary case this solves, and record_self()
    makes it possible to solve *safely*: only a process that /proc itself
    confirms is another run.py for this exact ROOT gets a SIGINT. Anything
    else holding the port, this leaves alone and reports, because "free up
    the port" and "guess what else on this machine is expendable" are not
    the same request, and only the first one is safe to automate.

    Returns True if the port is free (or was freed); False if it's occupied
    by something we won't touch.
    """
    if not _port_is_open(host, port):
        return True

    stale_pid = _stale_self_pid()
    if stale_pid is not None:
        log(f"port {port} is held by a previous run of this app (pid {stale_pid}) — "
            f"ending it")
        try:
            os.kill(stale_pid, signal.SIGINT)
        except ProcessLookupError:
            return True
        for _ in range(60):                       # ~6s
            if not _port_is_open(host, port):
                return True
            time.sleep(0.1)
        log(f"pid {stale_pid} did not release the port in time", RED)
        return False

    owner = _find_port_owner(port)
    print(f"\n{RED}  Port {port} is already in use by something else.{OFF}")
    if owner:
        print(f"{DIM}    {owner}{OFF}")
    print(f"{DIM}  This isn't a leftover copy of this app — I can tell because "
          f"there's no pidfile\n  matching it, or the process it points to isn't "
          f"this app anymore. Find and stop\n  whatever that is yourself, or "
          f"start this on a different port:{OFF}")
    print(f"      {sys.executable} run.py --port {port + 1}\n")
    return False


# --- Exposure warning --------------------------------------------------------

def warn_about_exposure(host: str) -> None:
    """Say plainly what protects the app at this bind address.

    Read directly from DATA_HOME/auth/auth.json rather than importing server.auth: this
    runs before the app itself has proven it can even start, and must still
    make sense if server/ is what a bad update just broke.
    """
    if host in ("127.0.0.1", "localhost", "::1"):
        print(f"{DIM}  Listening on this machine only (or through an SSH tunnel). "
              f"./shellwalk setup opens it to the LAN.{OFF}\n")
        return

    try:
        auth_state = json.loads((DATA_HOME / "auth" / "auth.json").read_text())
    except (OSError, json.JSONDecodeError):
        auth_state = {}
    password_set = bool(auth_state.get("password_hash"))
    lan_open = bool(auth_state.get("lan_open"))

    if lan_open:
        print(
            f"\n{RED}  Heads up{OFF}\n"
            f"{DIM}  Binding to {host} puts this on your LAN, and \u201cAllow the "
            "LAN with no password\u201d is\n"
            "  switched on in Settings \u203a Access. Anyone on your network "
            f"gets a shell here, no password asked.{OFF}\n"
        )
    elif password_set:
        print(f"{DIM}  Bound to {host}. The LAN password in Settings \u203a "
              f"Access is required for anyone not on this machine.{OFF}\n")
    else:
        # Only reachable with a live setup password (see effective_host()).
        print(
            f"\n{DIM}  Bound to {host}. No LAN password yet: the setup password "
            "printed by ./shellwalk setup\n"
            "  works from one device on your network, once, and then asks for "
            f"the real password.{OFF}\n"
        )


# --- Preflight ---------------------------------------------------------------

REQUIRED = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "python_multipart": "python-multipart",
    "httpx": "httpx",
    "anyio": "anyio",
}

# Modules that must not merely be findable, but actually import. A package can
# be half-installed — present on disk, missing a subpackage — and find_spec()
# will happily say yes. anyio is the one that bites: without
# anyio._backends._asyncio every static file and every threadpool call fails at
# request time, so the app appears to start and then serves 500s with no CSS.
MUST_IMPORT = (
    ("anyio._backends._asyncio", "anyio"),
    ("starlette.staticfiles", "starlette"),
)


def _other_interpreters() -> list[str]:
    """Python interpreters actually present, newest first, excluding this one.

    Never suggest `python3.12` on a machine that only has 3.14 — that is how a
    fix turns into a second dead end.
    """
    import shutil as _shutil
    current = f"python3.{sys.version_info.minor}"
    found = []
    for minor in (13, 12, 11):
        name = f"python3.{minor}"
        if name != current and _shutil.which(name):
            found.append(name)
    return found


def _pep668() -> bool:
    """True when pip refuses to install into this interpreter without a flag."""
    import sysconfig
    stdlib = sysconfig.get_paths().get("stdlib", "")
    return bool(stdlib) and Path(stdlib, "EXTERNALLY-MANAGED").exists()


def _offer_auto_setup() -> bool:
    """Build the virtualenv and install dependencies, with permission first.

    Two ways to grant that permission, matching the two situations this
    actually runs in:

      - a person watching a real terminal: ask, plainly, before touching
        anything, and take a blank Enter as yes — the common case is
        confirming a thing you already intended to do.
      - systemd, cron, or any other non-interactive start: there is no one to
        ask, and a prompt with nobody to answer it just hangs forever. This
        path only runs if SHELLWALK_AUTO_SETUP=1 (or --auto-setup) was set
        explicitly beforehand — permission given in advance, in writing,
        rather than assumed because no one was watching.

    Either way this runs exactly one thing, setup.sh, which already builds a
    fresh virtualenv, upgrades pip inside it, installs requirements.txt, and
    verifies the result by actually importing each package. Duplicating that
    here in Python would just be a second copy to keep in sync.
    """
    auto = os.environ.get("SHELLWALK_AUTO_SETUP") == "1" or "--auto-setup" in sys.argv[1:]
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if not auto and not interactive:
        return False   # nobody to ask, and no standing permission — leave it alone

    print(f"{DIM}  This will run: bash setup.sh{OFF}")
    print(f"{DIM}    - create .venv{OFF}")
    print(f"{DIM}    - upgrade pip inside it{OFF}")
    print(f"{DIM}    - install everything in requirements.txt{OFF}\n")

    if not auto:
        try:
            answer = input("  Set it up now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer not in ("", "y", "yes"):
            return False

    print()
    result = subprocess.run(["bash", str(ROOT / "setup.sh"), "--rebuild", sys.executable],
                            cwd=str(ROOT), env={**os.environ, "SHELLWALK_NO_WIZARD": "1"})
    if result.returncode != 0:
        print(f"\n{RED}  Setup did not finish cleanly (exit {result.returncode}). "
              f"See the output above.{OFF}")
        return False

    relaunch_into_venv(sys.argv[1:])   # never returns


def preflight() -> bool:
    """Check the app's dependencies before spawning it.

    Without this the app dies on its first import with a traceback, and
    under systemd it is restarted into the same traceback, forever. A missing dependency is not a crash
    to retry: it is a setup step, and the fix belongs on screen.
    """
    import importlib.util

    missing = [pkg for module, pkg in REQUIRED.items()
               if importlib.util.find_spec(module) is None]

    # Only worth checking once nothing is outright absent.
    broken: list[tuple[str, str, str]] = []
    if not missing:
        for module, package in MUST_IMPORT:
            try:
                importlib.import_module(module)
            except Exception as exc:            # ImportError, and anything odd
                broken.append((module, package, f"{type(exc).__name__}: {exc}"))

    if not missing and not broken:
        return True

    if broken:
        packages = sorted({package for _, package, _ in broken})
        print(f"\n{RED}  A required package is installed but incomplete: "
              f"{', '.join(packages)}{OFF}")
        for module, _, reason in broken:
            print(f"{DIM}    {module} -> {reason}{OFF}")
        print(f"\n{DIM}  This is a broken install, not a missing one. Reinstall it "
              f"cleanly:{OFF}")
        pip = (ROOT / ".venv" / "bin" / "pip") if (ROOT / ".venv").is_dir() else Path("pip3")
        print(f"      {pip} install --force-reinstall --no-cache-dir "
              f"{' '.join(packages)}\n")
        print(f"{DIM}  If that does not help, rebuild the environment from "
              f"scratch — an old pip{OFF}")
        print(f"{DIM}  inside the virtualenv is the usual cause of a "
              f"half-written package:{OFF}")
        print("      ./setup.sh\n")
        if sys.version_info >= (3, 14):
            alternatives = _other_interpreters()
            print(f"{DIM}  You are on Python {sys.version_info.major}."
                  f"{sys.version_info.minor}, which is very new, so some wheels "
                  f"may still be built{OFF}")
            print(f"{DIM}  from source and can fail half way.{OFF}")
            if alternatives:
                print(f"{DIM}  You also have {', '.join(alternatives)} installed. "
                      f"To use one:{OFF}")
                print(f"      ./setup.sh {alternatives[0]}\n")
            else:
                print(f"{DIM}  No older interpreter is installed here. If you want "
                      f"one:{OFF}")
                print("      sudo add-apt-repository ppa:deadsnakes/ppa")
                print("      sudo apt install python3.13-venv")
                print("      ./setup.sh python3.13\n")
        return False

    venv = ROOT / ".venv"
    print(f"\n{RED}  Missing Python packages: {', '.join(missing)}{OFF}")
    print(f"{DIM}  Shellwalk cannot start without them.{OFF}\n")

    # A successful install ends with relaunch_into_venv(), which replaces this
    # process outright — control never returns here on that path. It returns
    # only when nothing was fixed (declined, non-interactive, or the install
    # itself failed), in which case the manual instructions below still apply.
    _offer_auto_setup()

    if venv.is_dir():
        print(f"{DIM}  There is a virtualenv here already. Use it:{OFF}")
        print(f"      {venv}/bin/pip install -r requirements.txt")
        print(f"      {venv}/bin/python run.py\n")
    else:
        print(f"{DIM}  Recommended \u2014 a virtualenv, which never fights the system Python:{OFF}")
        print("      python3 -m venv .venv")
        print("      .venv/bin/pip install -r requirements.txt")
        print("      .venv/bin/python run.py\n")
        if _pep668():
            print(f"{DIM}  Or install system-wide. This Python is externally managed"
                  f" (PEP 668),{OFF}")
            print(f"{DIM}  so pip needs to be told explicitly:{OFF}")
            print("      pip3 install --break-system-packages -r requirements.txt\n")
        else:
            print(f"{DIM}  Or system-wide:{OFF}")
            print("      pip3 install -r requirements.txt\n")
    return False


# --- Running the app ------------------------------------------------------------

def run_app(host: str, port: int, argv: list[str]) -> int:
    """Run the app in this process until it stops.

    A restart never returns here: the app re-executes run.py in place
    (server/main.py, restart_in_place), with the argv saved below.
    """
    os.environ["SHELLWALK_BIND_HOST"] = host     # the gate's loopback-trust rule
    os.environ["SHELLWALK_PORT"] = str(port)     # for the printed recovery link
    os.environ["SHELLWALK_ARGV"] = json.dumps(argv)
    import uvicorn
    uvicorn.run("server.main:app", host=host, port=port, log_level="warning")
    return 0


SETTINGS_FILE = DATA_HOME / "settings.json"
AUTH_FILE = DATA_HOME / "auth" / "auth.json"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8022


def _saved_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def lan_exposure_allowed() -> bool:
    """The same rule as server/auth.py's lan_exposure_allowed(), read from
    auth.json directly for the same reason as everything else here.
    tests/test_core.py checks that the two agree."""
    try:
        data = json.loads(AUTH_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        return False
    if data.get("password_hash") or data.get("lan_open"):
        return True
    try:
        expires = float(data.get("bootstrap_expires_at", 0))
    except (TypeError, ValueError):
        expires = 0.0
    return bool(data.get("bootstrap_hash")) and time.time() < expires


_refused_hosts: set[str] = set()


def effective_host(requested: str) -> str:
    """Never listen beyond this machine while nothing protects it there.

    Enforced here at every start, whatever settings.json or --host say: with
    no LAN password, no live setup password and no deliberate lan_open, the
    server binds to 127.0.0.1 and says so."""
    if requested in LOOPBACK_HOSTS or lan_exposure_allowed():
        return requested
    if requested in _refused_hosts:      # resolved more than once per start
        return "127.0.0.1"
    _refused_hosts.add(requested)
    print(f"\n{RED}  Not listening on {requested}: no LAN password is set.{OFF}\n"
          f"{DIM}  Without one, anyone on your network would reach a shell here, so "
          f"Shellwalk stays on\n  127.0.0.1. Run ./shellwalk setup to open it to the "
          f"LAN with a setup password.{OFF}\n", flush=True)
    return "127.0.0.1"


def resolve_port(cli_port: int | None) -> int:
    if cli_port is not None:
        return cli_port
    port = _saved_settings().get("port")
    if isinstance(port, int) and 0 < port < 65536:
        return port
    return DEFAULT_PORT


def resolve_host(cli_host: str | None) -> str:
    """--host on the command line always wins — an explicit flag typed by a
    person at a terminal is a stronger signal than anything saved. Absent
    that, read bind_host fresh from DATA_HOME/settings.json every time this is
    called, not once at startup: the Settings panel changes that file and
    then asks for a restart (see /api/network/bind), which re-executes
    run.py, so the next start reads the new value here.

    Read directly rather than importing server.config: this runs before
    preflight() has confirmed the app's own dependencies are even
    installed, so nothing here can assume server/ is safely importable yet.
    """
    if cli_host is not None:
        return effective_host(cli_host)
    host = _saved_settings().get("bind_host")
    if isinstance(host, str) and host:
        return effective_host(host)
    return "127.0.0.1"


COMMANDS = ("setup", "reset-auth", "status", "link")


def run_command(name: str, rest: list[str]) -> int:
    """`run.py setup`, `run.py reset-auth`, `run.py status`: server/wizard.py.

    Dependencies are checked first, the same as for a normal start, so a
    missing package is explained here too instead of as a traceback."""
    if "-h" not in rest and "--help" not in rest:
        prepare_data_home()
        if not preflight():
            return 1
    from server import wizard
    return wizard.main([name, *rest])


def main() -> int:
    parser = argparse.ArgumentParser(description="Shellwalk")
    parser.add_argument("--host", default=None,
                        help="Overrides the saved bind_host (settings.json in "
                             "$SHELLWALK_HOME, by default ~/.config/shellwalk) "
                             "for this run.")
    parser.add_argument("--port", type=int, default=None,
                        help="Overrides the saved port (default 8022) for this run.")
    # Accepted and ignored: the app always runs in this process now.
    parser.add_argument("--no-supervisor", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--auto-setup", action="store_true",
                        help="Install missing dependencies without asking. "
                             "For non-interactive starts (systemd, cron); "
                             "same effect as SHELLWALK_AUTO_SETUP=1.")
    # `kill`, `systemctl stop` with the default signal, a shutdown: stop the
    # same way Ctrl+C does. While the app runs, uvicorn has its own handlers.
    def _terminate(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, signal.default_int_handler)

    if sys.argv[1:2] and sys.argv[1] in COMMANDS:
        return run_command(sys.argv[1], sys.argv[2:])
    args = parser.parse_args()
    args.port = resolve_port(args.port)
    argv = sys.argv[1:]

    # An update armed before a power cut is applied on the next start.
    if apply_pending_update():
        relaunch_self(argv)

    # Before anything below reads settings from DATA_HOME or writes into it.
    prepare_data_home()

    if not preflight():
        return 1

    startup_host = resolve_host(args.host)
    if not ensure_port_free(startup_host, args.port):
        return 1
    record_self()
    try:
        return _start(args, argv)
    finally:
        # Best-effort: a pidfile pointing at a PID that's already gone is
        # harmless (_stale_self_pid() checks liveness before trusting it),
        # but there's no reason to leave one lying around after a clean exit.
        try:
            if json.loads(PIDFILE.read_text()).get("pid") == os.getpid():
                PIDFILE.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError, KeyError):
            pass


def _start(args: argparse.Namespace, argv: list[str]) -> int:
    host = resolve_host(args.host)
    warn_about_exposure(host)
    location = (f"http://127.0.0.1:{args.port}" if host in LOOPBACK_HOSTS
                else f"http://{host}:{args.port}")
    print(f"{BANNER}  Shellwalk{OFF} {DIM}{location}{OFF}")
    if host == "0.0.0.0":
        print(f"{DIM}  Also reachable on this machine's LAN address.{OFF}")
    print()
    try:
        return run_app(host, args.port, argv)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
