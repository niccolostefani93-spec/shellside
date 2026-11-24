"""Guided setup in the terminal: `./shellside setup`, `reset-auth`, `status`.

Runs over SSH on a headless box: three questions, then it prints where to
open Shellside and, when the LAN was chosen and no password exists yet, a
short-lived setup password for the first sign-in from another device (see
server/auth.py's bootstrap section).

Nothing here weakens an install that is already configured: an existing LAN
password is never replaced or shown, and no setup password is issued while
one exists. `reset-auth` is the deliberate way back to the unconfigured state.

Autostart is a systemd *user* service plus lingering, so it needs no sudo and
no path is hard-coded: the unit is written for this checkout and this user.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import auth
from .config import DATA_HOME, ROOT, load_settings, save_settings

AMBER = "\033[38;5;179m"
DIM = "\033[38;5;244m"
RED = "\033[38;5;167m"
GREEN = "\033[38;5;108m"
BOLD = "\033[1m"
OFF = "\033[0m"

SERVICE_NAME = "shellside.service"
LOOPBACK = "127.0.0.1"
LAN = "0.0.0.0"


def say(text: str = "", colour: str = "") -> None:
    print(f"{colour}{text}{OFF}" if colour else text, flush=True)


# ---------------------------------------------------------------- questions

def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"  {prompt} [{default}] ").strip()
    except EOFError:
        print()
        raise KeyboardInterrupt
    return answer or default


def ask_choice(prompt: str, options: list[str], default: int) -> int:
    for number, label in enumerate(options, 1):
        say(f"    {number}) {label}")
    while True:
        answer = _ask(prompt, str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say(f"  Type a number from 1 to {len(options)}.", RED)


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = _ask(prompt, hint).lower()
        if answer == hint.lower():
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        say("  Answer y or n.", RED)


def ask_port(default: int) -> int:
    while True:
        answer = _ask("Port", str(default))
        if answer.isdigit() and 1024 <= int(answer) <= 65535:
            return int(answer)
        say("  A number from 1024 to 65535.", RED)


# ----------------------------------------------------------------- network

def lan_addresses() -> list[str]:
    """The address other devices on the network reach this machine at: the
    one on the default route. Only when there is none, every private IPv4
    address (hostname -I), which also lists bridges such as Docker's, so it
    is the fallback and not the rule. Best effort: an empty list just means
    no address is printed."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))          # TEST-NET: nothing is sent
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    if not found:
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                 timeout=3).stdout
            found += out.split()
        except (OSError, subprocess.SubprocessError):
            pass
    result = []
    for text in found:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if (address.version == 4 and address.is_private and not address.is_loopback
                and not address.is_link_local and text not in result):
            result.append(text)
    return result


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((LOOPBACK, port)) == 0


# ---------------------------------------------------------------- autostart

def _runtime_dir() -> Path:
    return Path(f"/run/user/{os.getuid()}")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    # Outside a login session (cron, a system service, `su`) XDG_RUNTIME_DIR
    # is unset and systemctl --user cannot find the user manager by itself.
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR") and _runtime_dir().is_dir():
        env["XDG_RUNTIME_DIR"] = str(_runtime_dir())
    return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, env=env)


def systemd_present() -> bool:
    """systemd is the init system here, so autostart is worth offering."""
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir()


def systemd_user_available() -> bool:
    if not systemd_present():
        return False
    return _systemctl("show-environment").returncode == 0


def _user() -> str:
    return os.environ.get("USER") or Path.home().name


def ensure_linger() -> bool:
    """Lingering keeps this user's systemd manager running without a login:
    without it a user service starts at login and stops at logout, which on
    a headless box means "never" or "only while you are in SSH". Enabling it
    for yourself needs no sudo on most distributions. True once the user
    manager is reachable."""
    if shutil.which("loginctl") and not linger_enabled():
        subprocess.run(["loginctl", "enable-linger", _user()], capture_output=True)
        for _ in range(30):                          # it starts asynchronously
            if systemd_user_available():
                break
            time.sleep(0.1)
    return systemd_user_available()


def unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "systemd" / "user" / SERVICE_NAME


def unit_text() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    python = venv_python if venv_python.exists() else Path(sys.executable)
    return f"""[Unit]
Description=Shellside (web terminal and file manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart="{python}" "{ROOT / 'run.py'}" --auto-setup
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
# The supervisor stops cleanly on SIGINT (see run.py).
KillSignal=SIGINT
TimeoutStopSec=10

[Install]
WantedBy=default.target
"""


def autostart_enabled() -> bool:
    return (unit_path().is_file()
            and _systemctl("is-enabled", SERVICE_NAME).stdout.strip() == "enabled")


def service_active() -> bool:
    return _systemctl("is-active", SERVICE_NAME).stdout.strip() == "active"


def linger_enabled() -> bool:
    out = subprocess.run(["loginctl", "show-user", _user(), "--property=Linger", "--value"],
                         capture_output=True, text=True)
    return out.stdout.strip() == "yes"


def enable_autostart() -> bool:
    if not ensure_linger():
        say("  Could not reach your systemd user manager. Run this once, then "
            "./shellside setup again:", RED)
        say(f"      sudo loginctl enable-linger {_user()}")
        return False
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text())
    _systemctl("daemon-reload")
    result = _systemctl("enable", SERVICE_NAME)
    if result.returncode != 0:
        say(f"  Could not enable the service: {result.stderr.strip()}", RED)
        return False
    say(f"  Autostart on: {path}", GREEN)
    if linger_enabled():
        say("  It starts at boot, before anyone logs in.", DIM)
    else:
        say("  It starts when you log in. To start it at boot instead, run once:", DIM)
        say(f"      sudo loginctl enable-linger {_user()}")
    return True


def disable_autostart() -> None:
    if not unit_path().is_file() or not systemd_user_available():
        return
    _systemctl("disable", "--now", SERVICE_NAME)
    unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")
    say("  Autostart removed.", DIM)


# -------------------------------------------------------------------- setup

def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shellside setup",
        description="Guided setup. With no options it asks; options answer for it.")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--local", action="store_true", help="only this machine")
    where.add_argument("--lan", action="store_true", help="every device on the local network")
    parser.add_argument("--port", type=int, help="port to listen on (default 8022)")
    start = parser.add_mutually_exclusive_group()
    start.add_argument("--autostart", action="store_true", help="start at boot")
    start.add_argument("--no-autostart", action="store_true", help="do not start at boot")
    tunnel = parser.add_mutually_exclusive_group()
    tunnel.add_argument("--tunnel", action="store_true",
                        help="reached from the internet through a tunnel or proxy")
    tunnel.add_argument("--no-tunnel", action="store_true", help="no tunnel or proxy")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="take the default for anything not given, ask nothing")
    return parser.parse_args(argv)


def setup(argv: list[str]) -> int:
    args = _arguments(argv)
    interactive = sys.stdin.isatty() and not args.yes
    settings = load_settings()
    current_lan = settings.get("bind_host", LOOPBACK) not in ("127.0.0.1", "localhost", "::1")
    current_port = int(settings.get("port") or 8022)
    can_autostart = systemd_present()

    say()
    say("  Shellside setup", AMBER + BOLD)
    say(f"  Settings and passwords live in {DATA_HOME}", DIM)
    if auth.password_is_set():
        say("  A LAN password is already set. It stays as it is; to start over, "
            "run ./shellside reset-auth.", DIM)
    say()

    # 1. Where from
    if args.lan or args.local:
        lan = args.lan
    elif interactive:
        say("  1. Who should be able to open Shellside?", BOLD)
        lan = ask_choice("Choose", [
            "Only this machine (open it here, or through an SSH tunnel)",
            "Every device on my local network (phone, laptop...), with a password",
        ], 2 if current_lan else 1) == 2
        say()
    else:
        lan = current_lan

    # 2. Port
    if args.port is not None:
        if not 1024 <= args.port <= 65535:
            say("  The port has to be between 1024 and 65535.", RED)
            return 2
        port = args.port
    elif interactive:
        say("  2. Which port?", BOLD)
        port = ask_port(current_port)
        say()
    else:
        port = current_port

    # 3. Autostart
    if args.autostart or args.no_autostart:
        autostart = args.autostart
    elif interactive and can_autostart:
        say("  3. Start Shellside automatically when this machine boots?", BOLD)
        autostart = ask_yes_no("Start at boot?", True)
        say()
    else:
        autostart = autostart_enabled()
    # 4. Tunnel
    if args.tunnel or args.no_tunnel:
        tunnel = args.tunnel
    elif interactive:
        say("  4. Will you reach it from the internet through a tunnel or reverse",
            BOLD)
        say("     proxy on this machine (cloudflared, ngrok, nginx, socat...)?", BOLD)
        say("     Such a tunnel connects from 127.0.0.1, so Shellside then stops", DIM)
        say("     trusting 127.0.0.1 by itself: on this machine too you sign in once,", DIM)
        say("     with the link from ./shellside link.", DIM)
        tunnel = ask_yes_no("Tunnel or proxy?", auth.tunnel_mode())
        say()
    else:
        tunnel = auth.tunnel_mode()

    if autostart and not can_autostart:
        say("  systemd user services are not available here, so autostart is "
            "skipped. Start it by hand with ./shellside", RED)
        autostart = False

    save_settings({"bind_host": LAN if lan else LOOPBACK, "port": port})

    if tunnel:
        auth.set_tunnel_mode(True)
    elif auth.internet_password_active():
        if auth.tunnel_mode():
            say("  Tunnel mode stays on: an internet password is active "
                "(Settings \u203a Access).", DIM)
    else:
        auth.set_tunnel_mode(False)

    setup_password = None
    if lan and not auth.password_is_set() and not auth.lan_open():
        setup_password = auth.issue_bootstrap_password()
    elif not lan:
        auth.clear_bootstrap_password()      # nothing on the LAN to sign in to

    if autostart:
        autostart = enable_autostart()
    else:
        disable_autostart()

    _summary(lan, port, setup_password)

    # Start, or restart so the new address and port take effect. Last, and
    # without waiting: when this runs from a Shellside terminal, the restart
    # ends this very shell.
    if autostart:
        verb = "restart" if service_active() else "start"
        _systemctl("--no-block", verb, SERVICE_NAME)
        say(f"  Shellside is {'restarting' if verb == 'restart' else 'starting'} "
            f"as a service. Logs: journalctl --user -u shellside -f", DIM)
    elif port_in_use(port):
        say(f"  Something already listens on port {port}. If it is Shellside, "
            "restart it to apply", DIM)
        say("  these settings; otherwise run ./shellside setup again and pick "
            "another port.", DIM)
    else:
        say("  Start it with:", DIM)
        say("      ./shellside")
    say()
    return 0


def _summary(lan: bool, port: int, setup_password: str | None) -> None:
    say("  Done.", GREEN + BOLD)
    say()
    say("  Open Shellside at:", BOLD)
    say(f"      http://127.0.0.1:{port}        (on this machine)")
    if lan:
        addresses = lan_addresses()
        for address in addresses:
            say(f"      http://{address}:{port}" + " " * max(1, 17 - len(address))
                + "(from your network)")
        if not addresses:
            say(f"      http://<this machine's IP>:{port}   (from your network)")
    else:
        user = os.environ.get("USER") or "you"
        host = socket.gethostname()
        say(f"  From another computer, through SSH:", DIM)
        say(f"      ssh -L {port}:127.0.0.1:{port} {user}@{host}")
        say(f"      then open http://127.0.0.1:{port} there")
    say()

    if auth.tunnel_mode():
        say("  Tunnel mode is on: 127.0.0.1 is not trusted by itself. To sign in "
            "from", DIM)
        say("  a browser on this machine, open the link printed by: "
            "./shellside link", DIM)
        say()

    if lan:
        if setup_password:
            expires = time.strftime("%H:%M", time.localtime(auth.bootstrap_expires_at()))
            say("  Setup password (shown only now):", BOLD)
            say(f"      {AMBER}{BOLD}{setup_password}{OFF}")
            say(f"  Use it once, from a device on your network, before {expires}. "
                f"It then asks", DIM)
            say("  you to choose your real password. If it expires, run "
                "./shellside setup again.", DIM)
        elif auth.password_is_set():
            say("  Sign in from your network with your LAN password.", DIM)
        elif auth.lan_open():
            say("  “LAN without a password” is on (Settings › Access): "
                "anyone on your network gets a shell here.", RED)
        say()
        if shutil.which("ufw"):
            say(f"  If the ufw firewall is enabled, allow the port: "
                f"sudo ufw allow {port}/tcp", DIM)
            say()


# ------------------------------------------------------------- reset / status

def reset_auth(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="shellside reset-auth",
        description="Forget the LAN password and go back to the unconfigured state.")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    say()
    say("  This removes the LAN password, switches off “LAN without a password”,", BOLD)
    say("  signs out every browser, and makes Shellside listen on this machine only.")
    say("  Devices enrolled with an internet password are kept (revoke them in Settings).", DIM)
    if not args.yes:
        if not sys.stdin.isatty():
            say("  Not a terminal: pass --yes to confirm.", RED)
            return 2
        try:
            if not ask_yes_no("Go ahead?", False):
                say("  Nothing changed.")
                return 0
        except KeyboardInterrupt:
            say("\n  Nothing changed.")
            return 0
    auth.reset_access()
    save_settings({"bind_host": LOOPBACK})
    say("  Done. Shellside is back to its unconfigured state.", GREEN)
    say()
    if sys.stdin.isatty() and not args.yes:
        try:
            if ask_yes_no("Run setup now?", True):
                return setup([])
        except KeyboardInterrupt:
            say()
    say("  Run ./shellside setup to configure it again.", DIM)
    return 0


def status(argv: list[str]) -> int:
    settings = load_settings()
    host = settings.get("bind_host", LOOPBACK)
    port = settings.get("port", 8022)
    say()
    say(f"  Data         {DATA_HOME}")
    say(f"  Listens on   {host}:{port}"
        + ("" if host in ("127.0.0.1", "::1") or auth.lan_exposure_allowed()
           else "  (falls back to 127.0.0.1: nothing protects the LAN)"))
    say(f"  LAN password {'set' if auth.password_is_set() else 'not set'}")
    if auth.bootstrap_active():
        expires = time.strftime("%H:%M", time.localtime(auth.bootstrap_expires_at()))
        say(f"  Setup pass.  active until {expires}")
    if auth.lan_open():
        say("  LAN open     yes: no password asked on the LAN", RED)
    say(f"  Tunnel mode  {'on: 127.0.0.1 must sign in too' if auth.tunnel_mode() else 'off: 127.0.0.1 is trusted'}")
    if systemd_present():
        say(f"  Autostart    {'on' if autostart_enabled() else 'off'}"
            + (f", service {'running' if service_active() else 'stopped'}"
               if unit_path().is_file() else ""))
    say(f"  Running      {'yes' if port_in_use(int(port)) else 'no'}")
    say()
    return 0


def link(argv: list[str]) -> int:
    """The proof-of-locality link (server/auth.py), read from this user's
    own files: being able to run this is the proof. Kept out of the logs on
    purpose, so this is the way to get it when Shellside runs as a service."""
    parser = argparse.ArgumentParser(
        prog="shellside link",
        description="Print the link that signs in a browser on this machine.")
    parser.add_argument("--open", action="store_true", help="also open it in the browser")
    args = parser.parse_args(argv)
    port = int(load_settings().get("port") or 8022)
    token = auth.local_token()
    if not port_in_use(port) or not token:
        say(f"  Shellside is not running on port {port}. Start it first.", RED)
        return 1
    url = f"http://127.0.0.1:{port}/auth/local?token={token}"
    say()
    say("  Open this in a browser on this machine (it only works from here):", DIM)
    say(f"      {url}")
    say("  It changes every time Shellside restarts.", DIM)
    say()
    if args.open:
        import webbrowser
        webbrowser.open(url)
    return 0


COMMANDS = {"setup": setup, "reset-auth": reset_auth, "status": status, "link": link}


def main(argv: list[str]) -> int:
    name, rest = argv[0], argv[1:]
    try:
        return COMMANDS[name](rest)
    except KeyboardInterrupt:
        say("\n  Stopped.", DIM)
        return 130
