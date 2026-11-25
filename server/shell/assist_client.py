"""Print the command assistant's answer into the terminal.

Run by server/shell/bashrc as:

    python3 -I assist_client.py not_found|ask <cwd> <words...>

Standard library only, because it runs with whatever Python started
Shellwalk and must never fail on a missing package.

Exit codes, read by bashrc:
    0    an answer (or an error worth reading) was printed, together with the
         "command not found" line
    3    the assistant is off or unavailable and printed nothing of note:
         bashrc says "command not found" the usual way
    130  interrupted with Ctrl-C
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

DIM = "\x1b[2m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
GREEN_UNDERLINE = "\x1b[32;4m"
RESET = "\x1b[0m"
CLEAR_LINE = "\r\x1b[2K"

SILENT = 3
TIMEOUT = 180


def link(command: str) -> str:
    """An OSC 8 hyperlink the browser turns into "paste this command"."""
    payload = base64.urlsafe_b64encode(command.encode()).decode().rstrip("=")
    return f"\x1b]8;;shellwalk-paste:{payload}\x1b\\{GREEN_UNDERLINE}{command}{RESET}\x1b]8;;\x1b\\"


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def request(mode: str, cwd: str, line: str) -> dict:
    body = json.dumps({"mode": mode, "cwd": cwd, "line": line,
                       "previous": os.environ.get("SHELLWALK_PREV_CMD", "").strip()}).encode()
    req = urllib.request.Request(
        os.environ["SHELLWALK_ASSIST_URL"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Shellwalk-Shell": os.environ.get("SHELLWALK_ASSIST_ID", ""),
            "X-Shellwalk-Token": os.environ.get("SHELLWALK_ASSIST_TOKEN", ""),
        },
        method="POST",
    )
    # Never through a proxy: the server is on this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in ("not_found", "ask"):
        return SILENT
    mode, cwd, words = argv[0], argv[1], argv[2:]
    line = " ".join(words)

    sys.stdout.write(f"{DIM}  … asking the model (Ctrl-C to stop){RESET}")
    sys.stdout.flush()
    try:
        answer = request(mode, cwd, line)
    except KeyboardInterrupt:
        sys.stdout.write(CLEAR_LINE)
        return 130
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stdout.write(CLEAR_LINE)
        if mode == "ask":
            out(f"{RED}  Shellwalk's assistant is unreachable: {exc}{RESET}")
            return 0
        return SILENT
    finally:
        sys.stdout.flush()
    sys.stdout.write(CLEAR_LINE)

    status = answer.get("status")
    if status == "disabled":
        # bashrc prints the usual "command not found" (or the distribution's
        # own handler does) after this.
        if answer.get("hint"):
            out(f"{DIM}  {answer['hint']}{RESET}")
        return SILENT
    if mode == "not_found":
        sys.stderr.write(f"bash: {words[0]}: command not found\n")
        sys.stderr.flush()
    if status == "error":
        out(f"{RED}  {answer.get('message', 'Something went wrong.')}{RESET}")
        return 0

    commands = 0
    for item in answer.get("items", []):
        if item.get("type") == "command":
            out(f"  {CYAN}▸{RESET} {link(item['value'])}")
            commands += 1
        else:
            out(f"  {CYAN}{item.get('value', '')}{RESET}")
    if commands:
        out(f"{DIM}  tap a command to paste it, then press Enter{RESET}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
