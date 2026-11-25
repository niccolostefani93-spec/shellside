"""The command assistant: what happens when a command typed in the shell fails.

How a failed command reaches the model
--------------------------------------
Shellside starts bash with its own rc file (server/shell/bashrc), which sources
the user's usual ~/.bashrc and then defines bash's `command_not_found_handle`.
Bash calls that function only when the first word of a line is not a command,
which covers both typos (`makedire cane`) and plain-language requests
(`create a folder called cane`). Nothing is guessed from the terminal output.

The function runs server/shell/assist_client.py, which POSTs the line here and
prints the answer into the terminal as ordinary output. Suggested commands are
written as OSC 8 hyperlinks with a `shellside-paste:` URI; the browser turns a
click on one into a paste (never a run). Because the answer is real output it
is part of the scrollback, and it is there again after a reconnect.

`, <anything>` asks explicitly, for requests that begin with a real command
word (`, find the biggest files here`) and therefore never fail.

Why the endpoint has its own check
----------------------------------
The client runs inside the user's shell, with no browser cookie. The gate lets
it through only from loopback and only with a token that the server derives
from a key on disk and the shell's own id. The key lives in the data folder,
so a shell kept alive in tmux still holds a valid token after Shellside
restarts. Loopback alone is not enough: with a tunnel, internet traffic also
arrives from 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time
from typing import Any

from . import services
from .config import AUTH_DIR, DATA_DIR, ROOT, load_settings

ENDPOINT = "/internal/assist"
HEADER_ID = b"x-shellside-shell"
HEADER_TOKEN = b"x-shellside-token"

SHELL_DIR = ROOT / "server" / "shell"
BASHRC = SHELL_DIR / "bashrc"
CLIENT = SHELL_DIR / "assist_client.py"

# With the other secrets in DATA_HOME/auth/ (mode 0700), which backups and
# updates never touch. Earlier versions kept it in the data folder, where
# every backup zip picked it up; it is moved on first use.
_KEY_FILE = AUTH_DIR / "assist_key"
_OLD_KEY_FILE = DATA_DIR / "assist.key"
_key: bytes | None = None

# A loop such as `for f in *; do nope; done` must not fire one model call per
# iteration: a second automatic call from the same shell this soon after the
# previous one falls back to the plain "command not found".
_DEBOUNCE_SECONDS = 2.0
_last_call: dict[str, float] = {}
# Shells already told once that no model is set up; repeating it on every
# typo would be worse than never saying it.
_hinted: set[str] = set()

# Services that cannot actually answer. `echo` only repeats the prompt.
_NOT_A_MODEL = {None, "", "echo"}


# ------------------------------------------------------------------- tokens

def _secret() -> bytes:
    global _key
    if _key is None:
        try:
            _key = _KEY_FILE.read_bytes()
        except OSError:
            _key = b""
        if len(_key) < 32:
            # Keep the old key if there is one, so shells already running
            # (in tmux) still hold a valid token after the move.
            try:
                _key = _OLD_KEY_FILE.read_bytes()
            except OSError:
                _key = b""
            if len(_key) < 32:
                _key = secrets.token_bytes(32)
            _KEY_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(_key)
        try:
            _OLD_KEY_FILE.unlink()
        except OSError:
            pass
    return _key


def token_for(shell_id: str) -> str:
    return hmac.new(_secret(), shell_id.encode(), hashlib.sha256).hexdigest()


def verify_headers(headers) -> bool:
    shell_id = token = None
    for name, value in headers:
        if name == HEADER_ID:
            shell_id = value.decode("latin-1")
        elif name == HEADER_TOKEN:
            token = value.decode("latin-1")
    if not shell_id or not token:
        return False
    return hmac.compare_digest(token_for(shell_id), token)


def shell_environment(shell_id: str) -> dict[str, str]:
    """Variables a new shell needs to reach this endpoint."""
    port = os.environ.get("SHELLSIDE_PORT", "8022")
    return {
        "SHELLSIDE_ASSIST_URL": f"http://127.0.0.1:{port}{ENDPOINT}",
        "SHELLSIDE_ASSIST_ID": shell_id,
        "SHELLSIDE_ASSIST_TOKEN": token_for(shell_id),
        "SHELLSIDE_ASSIST_CLIENT": str(CLIENT),
        "SHELLSIDE_PYTHON": os.environ.get("SHELLSIDE_PYTHON_BIN") or _python(),
    }


def _python() -> str:
    import sys
    return sys.executable or "python3"


# ------------------------------------------------------------------- prompt

# Most people ask about their computer, Linux or the terminal, but a question
# on any other subject gets a plain answer rather than a refusal. The context
# comes first and the user's line last: small local models answer what they
# read last, and otherwise drift to the previous command instead. They also
# follow short, concrete rules best, and slip into a second language unless
# told plainly not to.
_CASES = """\
What to do, by what the line is:
- A typo of a real command: give the corrected command, and say in a few words what was wrong.
- A request in plain words for something to do on the computer: give the command or commands that do it.
- A question about the computer, Linux or the terminal, including about the previous \
command ("why did that fail?", "what did that do?"): answer it in one or two short \
sentences, with a command when one helps.
- A question on any other subject: answer it in at most three short sentences, with no commands.
- If you cannot tell what was meant, say so in one sentence and suggest nothing.
"""

_RULES = """\
Rules:
- Answer the user's line and nothing else. Mention the previous command only when the \
line asks about it.
- Write everything in the language of the user's line. Never add a translation \
or a sentence in another language.
- At most 3 commands, each on its own line wrapped in pipes, like |du -sh * | sort -h|. \
Only real commands that exist on Linux; never invent one. Nothing but a command goes \
between pipes.
- If a command deletes, overwrites or formats anything, say so plainly.
- When the line refers to files ("these", "the ones I see", "the 7 screenshots"), use the \
folder listing: name the exact files, quoted, or a pattern that matches exactly \
those and nothing else. Never use rm -r on plain files.
- Start straight with the answer. Never say which case it is, and never repeat \
these instructions.
- No markdown, no code fences, no headings.
"""

_INTRO = """\
You are the assistant built into Shellside, a terminal in the browser, on a Linux \
computer. Most questions are about the computer, Linux or the terminal, but any \
question is welcome.

Current folder: {cwd}
{context}
"""

_PREVIOUS = """\
Just before this line, the user ran this command; the terminal shows:

{block}
If the line says "it", "that", "the error", "doesn't work" or asks why something \
failed, it means this command.

"""

_OUTRO = """
Reply in the language of this line, with each command between pipes.
"""

PROMPT_NOT_FOUND = _INTRO + _CASES + "\n" + _RULES + "\n{previous}" + """\
The user typed this line in bash. Its first word is not a command, so bash \
could not run it. It is a mistyped command, or something written in plain words:

    {line}
""" + _OUTRO

PROMPT_ASK = _INTRO + _CASES + "\n" + _RULES + "\n{previous}" + """\
The user asks:

    {line}
""" + _OUTRO


def build_prompt(mode: str, line: str, cwd: str, listing: str, block: list[str]) -> str:
    previous = ""
    if block:
        previous = _PREVIOUS.format(block="\n".join(f"    {row}" for row in block) + "\n")
    template = PROMPT_NOT_FOUND if mode == "not_found" else PROMPT_ASK
    context = f"\n{listing}" if listing else ""
    return template.format(line=line, cwd=cwd or "unknown", context=context, previous=previous)


def parse_answer(text: str) -> list[dict[str, str]]:
    """Split a model answer into text lines and commands.

    The model is asked to put each command alone on a line between pipes.
    When it puts prose first ("Try this: |du -sh * | sort -h|"), a line that
    ends with a pipe is read as prose up to the first pipe, then one command
    up to the last pipe, inner pipes and all, because a shell pipeline is far
    more likely there than two separate commands. Anywhere else |...| pairs
    are matched non-greedily.
    """
    import re

    items: list[dict[str, str]] = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        first, last = line.find("|"), line.rfind("|")
        if line.endswith("|") and first != -1 and last > first + 1:
            prose, command = line[:first].strip(), line[first + 1:last].strip()
            if prose:
                items.append({"type": "text", "value": prose})
            if command:
                items.append({"type": "command", "value": command})
            continue
        found = list(re.finditer(r"\|([^|\n]+)\|", line))
        if not found:
            items.append({"type": "text", "value": line})
            continue
        prose = re.sub(r"\|([^|\n]+)\|", lambda m: m.group(1).strip(), line).strip()
        if prose:
            items.append({"type": "text", "value": prose})
        items.extend({"type": "command", "value": m.group(1).strip()} for m in found)
    # The same command offered twice is noise.
    seen: set[str] = set()
    unique = []
    for item in items:
        key = f"{item['type']}:{item['value']}"
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# ------------------------------------------------------------------ context
#
# What the user sees in the browser and the model otherwise would not: the
# folder panel's listing of the current folder, and the command run just
# before this line, with its output. With them, "delete the 7 screenshots" or
# "why did that fail?" can be answered with real names instead of a guess.
#
# Only that one command, not the whole screen: a small local model given
# thirty lines of earlier questions and answers latches on to them and
# answers those instead of the line in front of it.

_LISTING_LIMIT = 150
_OUTPUT_LINES = 30
_SCREEN_LINES = 400

def _ansi_pattern():
    import re
    # OSC (…BEL or …ST), CSI, and the other two-byte escapes.
    return re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

_ANSI = _ansi_pattern()


def _human_size(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _folder_listing(cwd: str) -> str:
    from . import fsview
    if not cwd:
        return ""
    try:
        listing = fsview.list_dir(cwd)
    except Exception:
        return ""
    if listing.get("error"):
        return ""
    entries = listing.get("entries", [])
    if not entries:
        return "Folder listing (as shown in the user's file panel): empty.\n"
    lines = []
    for entry in entries[:_LISTING_LIMIT]:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["mtime"]))
        if entry["dir"]:
            lines.append(f"    {entry['name']}/    {stamp}")
        else:
            lines.append(f"    {entry['name']}    {_human_size(entry['size'])}    {stamp}")
    more = len(entries) - _LISTING_LIMIT
    if more > 0:
        lines.append(f"    … and {more} more")
    hidden = listing.get("hidden_count", 0)
    note = f" ({hidden} hidden files not listed)" if hidden else ""
    return ("Folder listing, as shown in the user's file panel" + note + ":\n"
            + "\n".join(lines) + "\n")


def _screen_lines(shell_id: str) -> list[str]:
    """The terminal's recent lines as plain text, oldest first.

    In tmux the bytes Shellside receives are tmux redrawing its window with
    cursor moves, so the text is asked of tmux itself. Otherwise it is the
    shell's own output, kept in the session's scrollback.
    """
    from . import pty_session
    session = pty_session.registry.get(shell_id)
    if session is None:
        return []
    text = ""
    if session.tmux:
        try:
            result = pty_session._tmux("capture-pane", "-p", "-J", "-S", f"-{_SCREEN_LINES}",
                                       "-t", f"={pty_session.tmux_name(shell_id)}:")
            if result.returncode == 0:
                text = result.stdout
        except Exception:
            text = ""
    if not text:
        raw = session.scrollback()[-64000:].decode("utf-8", errors="replace")
        text = _ANSI.sub("", raw)
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.rsplit("\r", 1)[-1].rstrip()   # a \r redraws the line: keep what is left
        if line and "asking the model" not in line:
            lines.append(line)
    return lines


def last_command_block(lines: list[str], previous: str) -> list[str]:
    """The previous command's prompt line and its output, from the screen.

    `lines` ends with the line being asked about. `previous` is the command
    bash ran before it, from its history. The block starts at the last line
    that ends with that command (its prompt line) and stops before the
    current line. If the command is not on screen (the terminal was cleared,
    or it came from an earlier session's history), there is no block.
    """
    previous = previous.strip().split("\n", 1)[0].strip()
    lines = lines[:-1]               # the current line is given on its own
    if not previous:
        return []
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].rstrip().endswith(previous):
            output = lines[index + 1:]
            if len(output) > _OUTPUT_LINES:
                output = ["    … (earlier output left out)"] + output[-_OUTPUT_LINES:]
            return [lines[index]] + output
    return []


def context_for(shell_id: str, cwd: str, previous: str = "") -> tuple[str, list[str]]:
    """The folder listing and the previous command's block, or nothing when
    the user turned sharing off."""
    if not load_settings().get("assist_share_context", True):
        return "", []
    return _folder_listing(cwd), last_command_block(_screen_lines(shell_id), previous)


# ----------------------------------------------------------------- handling

def model_name() -> str | None:
    service_id = services.active_id("llm")
    return None if service_id in _NOT_A_MODEL else service_id


async def handle(payload: dict[str, Any], shell_id: str) -> dict[str, Any]:
    """Answer one request from assist_client.py.

    Returns {"status": "disabled"} when the shell should behave as if the
    assistant did not exist, {"status": "error", "message": ...} for a problem
    worth showing, or {"status": "ok", "items": [...]} with the answer.
    """
    mode = payload.get("mode")
    line = str(payload.get("line", "")).strip()[:2000]
    cwd = str(payload.get("cwd", ""))[:1000]
    previous = str(payload.get("previous", ""))[:2000]
    if mode not in ("not_found", "ask") or not line:
        return {"status": "error", "message": "Nothing to ask."}

    if mode == "not_found":
        if not load_settings().get("assist_on_not_found", True):
            return {"status": "disabled"}
        now = time.monotonic()
        if now - _last_call.get(shell_id, 0.0) < _DEBOUNCE_SECONDS:
            _last_call[shell_id] = now
            return {"status": "disabled"}
        _last_call[shell_id] = now

    service = services.get("llm")
    if model_name() is None or service is None or not hasattr(service, "ask"):
        if mode == "not_found":
            if shell_id in _hinted:
                return {"status": "disabled"}
            _hinted.add(shell_id)
            return {"status": "disabled", "hint": "No model is set up for suggestions: "
                    "pick one in Settings › Model, or turn suggestions off there."}
        return {"status": "error", "message": "No model is set up. Pick one in Settings › Model."}

    listing, block = context_for(shell_id, cwd, previous)
    prompt = build_prompt(mode, line, cwd, listing, block)
    try:
        result = service.ask(prompt, "")
        if asyncio.iscoroutine(result):
            result = await result
    except Exception as exc:  # a broken service must not break the shell
        return {"status": "error", "message": f"The model request failed: {exc}"}

    items = parse_answer(str(result or ""))
    if not items:
        return {"status": "error", "message": "The model returned an empty answer."}
    return {"status": "ok", "model": model_name(), "items": items}
