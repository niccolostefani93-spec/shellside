"""Real PTY sessions.

Two things here are worth reading carefully, because they are where this
implementation deliberately diverges from the handoff document:

1. Current working directory is resolved by reading /proc/<pid>/cwd for the
   PTY's *foreground process group*, never by parsing `cd` out of the bytes we
   write into stdin. Parsing would miss `cd -`, `pushd`, aliases, `cd $VAR`,
   shell functions, and anything the user types that we did not originate.
   The /proc symlink is the kernel's own answer and is always correct.

2. Text injection ("as if typed") checks whether the shell is actually sitting
   at its prompt first. If vim or a REPL owns the terminal, injecting
   `rm -rf /path` types that into vim. `os.tcgetpgrp(fd) == shell_pid` tells us
   the shell itself is in the foreground, because pty.fork() makes the child a
   session leader whose pgid equals its pid.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import pty
import signal
import shutil
import struct
import subprocess
import termios
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable

from . import assist, datahome
from .config import DATA_HOME

# Control characters we write into stdin.
CTRL_U = b"\x15"  # readline: kill whole line

# ------------------------------------------------------------------- tmux
#
# Optional (Settings › General › Keep shells in tmux). A shell in tmux belongs
# to tmux's server process, not to Shellwalk, so it survives Shellwalk being
# updated or restarted; the browser tab reattaches by the same session id.
#
# Shellwalk uses its own tmux server (-L shellwalk) and its own config file,
# so the user's tmux sessions and ~/.tmux.conf are never touched.
#
# One tmux server per DATA_HOME: a second Shellwalk of the same user (a test
# install with its own SHELLWALK_HOME) must not list, and attach to, the
# shells of the first. The default DATA_HOME keeps the plain name, so shells
# started before this rule still reattach.

TMUX_CONF = Path(__file__).resolve().parent / "shell" / "tmux.conf"
TMUX_PREFIX = "shellwalk-"


def tmux_socket_name(data_home: Path) -> str:
    if data_home.absolute() == datahome.default_data_home().absolute():
        return "shellwalk"
    digest = hashlib.sha256(str(data_home.absolute()).encode()).hexdigest()[:10]
    return f"shellwalk-{digest}"


TMUX_SOCKET = tmux_socket_name(DATA_HOME)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux(*args: str, timeout: float = 3.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TMUX", None)   # Shellwalk itself started from inside tmux
    return subprocess.run(["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def tmux_name(session_id: str) -> str:
    return f"{TMUX_PREFIX}{session_id}"


def tmux_sessions() -> list[dict]:
    """Shellwalk's tmux sessions, whether or not Shellwalk is attached to them."""
    if not tmux_available():
        return []
    try:
        result = _tmux("list-sessions", "-F",
                       "#{session_name}\t#{session_created}\t#{pane_current_path}"
                       "\t#{pane_current_command}\t#{session_attached}")
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:          # no server running yet
        return []
    sessions = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or not parts[0].startswith(TMUX_PREFIX):
            continue
        sessions.append({
            "id": parts[0][len(TMUX_PREFIX):],
            "created": float(parts[1] or 0),
            "cwd": parts[2] or None,
            "running": parts[3] or None,
            "attached": parts[4] not in ("", "0"),
        })
    return sessions


def tmux_has(session_id: str) -> bool:
    if not tmux_available():
        return False
    try:
        return _tmux("has-session", "-t", f"={tmux_name(session_id)}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def shell_argv(shell: str) -> list[str]:
    """bash gets Shellwalk's rc file (which loads ~/.bashrc first); other
    shells start exactly as before."""
    if os.path.basename(shell) == "bash" and assist.BASHRC.is_file():
        return [shell, "--rcfile", str(assist.BASHRC), "-i"]
    return [shell, "-i"]


class PtySession:
    def __init__(
        self,
        shell: str,
        cwd: str,
        scrollback_bytes: int = 200_000,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
        use_tmux: bool = False,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.shell = shell
        # The tab's name, kept here so every browser shows the same one.
        self.title: str | None = None
        # Called once when the shell is ended on purpose (its tab closed,
        # here or on another device), so open terminals drop that tab.
        self._close_callbacks: set[Callable[[], None]] = set()
        self.created = time.time()
        self.alive = True
        self.tmux = bool(use_tmux and tmux_available())
        self._scrollback_limit = scrollback_bytes
        self._scrollback: deque[bytes] = deque()
        self._scrollback_size = 0
        self._listeners: set[Callable[[bytes], None]] = set()
        self._reader_installed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        # With tmux, the shell is not our child but runs in a tmux pane:
        # its pid, once tmux reports it (see shell_pid()).
        self._pane_pid: int | None = None

        # What the shell itself needs: a marker, plus the assistant's address.
        shell_env = {"SHELLWALK": "1", "COLORTERM": "truecolor",
                     **assist.shell_environment(self.id)}

        environ = dict(os.environ)
        environ.pop("TMUX", None)
        environ.update({"TERM": "xterm-256color", **shell_env})
        if env:
            environ.update(env)

        start_dir = cwd if os.path.isdir(cwd) else os.path.expanduser("~")
        argv = shell_argv(shell)
        if self.tmux:
            # -A: attach when the session already exists (Shellwalk restarted
            # and the tab reconnects), create it otherwise.
            extra = [arg for key, value in shell_env.items() for arg in ("-e", f"{key}={value}")]
            argv = ["tmux", "-L", TMUX_SOCKET, "-f", str(TMUX_CONF),
                    "new-session", "-A", "-s", tmux_name(self.id), "-c", start_dir,
                    *extra, *argv]

        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(start_dir)
                os.execvpe(argv[0], argv, environ)
            except Exception:  # pragma: no cover - child process
                os._exit(1)
        self.pid = pid
        self.fd = fd
        os.set_blocking(fd, False)

    # ---------------------------------------------------------------- output

    def add_listener(self, fn: Callable[[bytes], None]) -> None:
        self._listeners.add(fn)

    def listener_count(self) -> int:
        return len(self._listeners)

    def remove_listener(self, fn: Callable[[bytes], None]) -> None:
        self._listeners.discard(fn)

    def add_close_callback(self, fn: Callable[[], None]) -> None:
        self._close_callbacks.add(fn)

    def remove_close_callback(self, fn: Callable[[], None]) -> None:
        self._close_callbacks.discard(fn)

    def attach_to_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._reader_installed or not self.alive:
            return
        loop.add_reader(self.fd, self._on_readable)
        self._loop = loop
        self._reader_installed = True

    def _remove_reader(self) -> None:
        """Stop watching our fd. Always before the fd is closed.

        The event loop keeps its own table of fds to watch. Closing the fd
        without this leaves a stale entry under that number, and the kernel
        hands the number out again to the next thing opened: another tab's
        PTY, a websocket, a file. The loop then calls our reader for that, or
        trips over the stale entry, in a tight loop. That was the 100% CPU.
        """
        if not self._reader_installed:
            return
        self._reader_installed = False
        loop = self._loop
        self._loop = None
        try:
            if loop is not None and not loop.is_closed():
                loop.remove_reader(self.fd)
        except (OSError, ValueError, RuntimeError):
            pass

    def _on_readable(self) -> None:
        if not self.alive:
            self._remove_reader()
            return
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            self._mark_dead()
            return
        if not data:
            self._mark_dead()
            return
        self._remember(data)
        for fn in list(self._listeners):
            try:
                fn(data)
            except Exception:
                pass

    def _remember(self, data: bytes) -> None:
        self._scrollback.append(data)
        self._scrollback_size += len(data)
        while self._scrollback_size > self._scrollback_limit and self._scrollback:
            self._scrollback_size -= len(self._scrollback.popleft())

    def scrollback(self) -> bytes:
        return b"".join(self._scrollback)

    def _mark_dead(self) -> None:
        # The reader goes even if the session was already marked dead.
        self._remove_reader()
        if not self.alive:
            return
        self.alive = False
        for fn in list(self._listeners):
            try:
                fn(b"\r\n[shellwalk] shell exited\r\n")
            except Exception:
                pass

    # ----------------------------------------------------------------- input

    def write(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            self._mark_dead()

    def inject(
        self,
        text: str,
        execute: bool = False,
        clear_line: bool = True,
        require_idle: bool = True,
    ) -> tuple[bool, str]:
        """Type `text` into the shell as if the user had typed it.

        Returns (ok, reason). Refuses when the shell is not at its prompt and
        require_idle is set, so a click never lands inside vim or a REPL.
        """
        if not self.alive:
            return False, "The shell has exited. Open a new session."
        if require_idle and not self.is_idle():
            running = self.foreground_command() or "a program"
            return False, f"Terminal is busy running {running}. Exit it first."
        payload = b""
        if clear_line:
            payload += CTRL_U
        payload += text.encode()
        if execute:
            payload += b"\r"
        self.write(payload)
        return True, ""

    def resize(self, rows: int, cols: int) -> None:
        if not self.alive:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    # ------------------------------------------------------------ inspection

    def shell_pid(self) -> int | None:
        """The shell's pid: our child, or the shell in tmux's pane."""
        if not self.tmux:
            return self.pid
        if self._pane_pid is None:
            try:
                result = _tmux("display-message", "-p", "-t", f"={tmux_name(self.id)}:",
                               "#{pane_pid}", timeout=1.0)
                self._pane_pid = int(result.stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError):
                return None             # tmux still starting; asked again next time
        return self._pane_pid

    def foreground_pgid(self) -> int | None:
        if not self.tmux:
            try:
                return os.tcgetpgrp(self.fd)
            except OSError:
                return None
        # The pane's tty is not ours, so tcgetpgrp() cannot ask it. The
        # kernel reports the same value in /proc/<pid>/stat as tpgid: the
        # foreground process group of that process's terminal.
        pid = self.shell_pid()
        if pid is None:
            return None
        try:
            stat = open(f"/proc/{pid}/stat").read()
            tpgid = int(stat[stat.rindex(")") + 2:].split()[5])
        except (OSError, ValueError, IndexError):
            self._pane_pid = None       # pane gone or replaced: look it up again
            return None
        return tpgid if tpgid > 0 else None

    def is_idle(self) -> bool:
        """True when the shell itself owns the terminal (i.e. at the prompt)."""
        pgid = self.foreground_pgid()
        return pgid is not None and pgid == self.shell_pid()

    def foreground_command(self) -> str | None:
        pgid = self.foreground_pgid()
        if pgid is None:
            return None
        try:
            comm = open(f"/proc/{pgid}/comm").read().strip()
            return comm or None
        except OSError:
            return None

    def cwd(self) -> str | None:
        """Resolve the cwd of whatever currently owns the terminal.

        Prefers the foreground process (so `cd` inside a subshell is reflected
        while it runs), and falls back to the shell itself.
        """
        for pid in (self.foreground_pgid(), self.shell_pid()):
            if not pid:
                continue
            try:
                return os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
        return None

    def close(self) -> None:
        """End the shell for good (closing its tab), tmux session included.

        Safe to call more than once. It must be: closing an fd number twice
        could close whatever the kernel has since given that number to.
        """
        if self._closed:
            return
        self._closed = True
        self.alive = False
        self._remove_reader()           # before os.close(): see _remove_reader()
        self._listeners.clear()
        for fn in list(self._close_callbacks):
            try:
                fn()
            except Exception:
                pass
        self._close_callbacks.clear()
        if self.tmux:
            try:
                _tmux("kill-session", "-t", f"={tmux_name(self.id)}")
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            os.killpg(os.getpgid(self.pid), signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        # Reap the child so it does not linger as a zombie.
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            pass


class SessionRegistry:
    """Keeps sessions alive across websocket reconnects.

    This matters more than it sounds for the stated use case: a phone on mobile
    data drops its socket constantly, and losing the shell every time would make
    the tool unusable while travelling. Reconnecting replays scrollback.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}

    def get(self, session_id: str | None) -> PtySession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session and not session.alive:
            # A shell that exited on its own: release its fd too.
            self._sessions.pop(session_id, None)
            session.close()
            return None
        return session

    def create(self, **kwargs) -> PtySession:
        session = PtySession(**kwargs)
        self._sessions[session.id] = session
        return session

    def adopt(self, session_id: str, **kwargs) -> PtySession | None:
        """Reattach to a shell that outlived Shellwalk in tmux, keeping its id.

        Returns None when there is no such tmux session, so the caller opens
        a new shell as usual.
        """
        if not session_id or session_id in self._sessions or not tmux_has(session_id):
            return None
        kwargs.update(session_id=session_id, use_tmux=True)
        return self.create(**kwargs)

    def drop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()

    def all(self) -> list[PtySession]:
        return list(self._sessions.values())

    def workspace(self, **kwargs) -> list[PtySession]:
        """The open tabs: every live shell, oldest first.

        Tabs live here rather than in the browser, so closing the browser
        leaves them all running and whichever device opens Shellwalk next,
        phone or computer, gets the same tabs back. Shells that outlived a
        Shellwalk restart in tmux are taken back in first.
        """
        for item in tmux_sessions():
            if item["id"] not in self._sessions:
                self.adopt(item["id"], **kwargs)
        self.reap()
        return sorted(self._sessions.values(), key=lambda session: session.created)

    def reap(self) -> None:
        for sid, session in list(self._sessions.items()):
            if not session.alive:
                self._sessions.pop(sid, None)
                session.close()


registry = SessionRegistry()
