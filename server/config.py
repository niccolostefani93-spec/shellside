"""Paths and persisted settings for Shellwalk.

Configuration lives on disk, never only in the running process. It is split
in two places (see server/datahome.py for why):

  ROOT       the code directory. Read-only at runtime: what ships, including
             the defaults that seed a new install and the settings panel and
             context menu, read from here so an update reaches you.
  DATA_HOME  everything the app writes: settings, saved commands, favorites,
             service choices, your own menu actions and scripts, credentials.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import datahome

# Bumped whenever the shape of the app changes in a way worth telling apart.
# Printed at startup and returned by /api/bootstrap, so "which build is
# actually serving me right now" is never a guess — a stale process still
# holding the port looks identical to a new one otherwise.
VERSION = "0.2.0"

ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = ROOT / "services"
WEB_DIR = ROOT / "web"

# Shipped, in the code directory. app/ keeps its name rather than becoming
# defaults/: the update-from-zip contract (update_plan.MERGE_ROOTS, and the
# copy in every already-installed run.py) names "app", and an older run.py
# would refuse to write an update into a folder it has never heard of.
APP_DIR = ROOT / "app"
SCRIPTS_DIR = APP_DIR / "scripts"              # read; your layer is USER_SCRIPTS_DIR
CONTEXT_MENU_DIR = APP_DIR / "context_menu"    # read; your layer is USER_CONTEXT_MENU_DIR
SHIPPED_COMMANDS_DIR = APP_DIR / "commands"    # only seeds COMMANDS_DIR

# Yours, outside the code directory.
DATA_HOME = datahome.resolve_data_home()
SETTINGS_FILE = DATA_HOME / "settings.json"
COMMANDS_DIR = DATA_HOME / "commands"
FAVORITES_FILE = DATA_HOME / "favorites" / "paths.json"
DATA_DIR = DATA_HOME / "data"
AUTH_DIR = DATA_HOME / "auth"                  # mode 0700, files 0600
USER_SCRIPTS_DIR = DATA_HOME / "scripts"
USER_CONTEXT_MENU_DIR = DATA_HOME / "context_menu"

# Here, at import, rather than in a startup hook: this module is imported
# before auth.py or anything else that can write into DATA_HOME, so the
# one-time copy of pre-DATA_HOME state always sees a DATA_HOME nobody has
# created yet. Under run.py this already ran there and finds nothing to do.
datahome.prepare(ROOT, DATA_HOME)

# Extensions the browser can render on its own (handoff doc section 3.7).
BROWSER_NATIVE: dict[str, str] = {
    # web / markup
    ".html": "text/html", ".htm": "text/html", ".xhtml": "application/xhtml+xml",
    ".xml": "application/xml", ".json": "application/json",
    # images
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif",
    # text / documents
    ".pdf": "application/pdf", ".txt": "text/plain", ".log": "text/plain",
    ".md": "text/plain",
    # audio
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".aac": "audio/aac", ".m4a": "audio/mp4", ".flac": "audio/flac",
    # video
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg",
    ".m4v": "video/mp4",
}

# Rendered inline only inside a sandboxed frame/tab: these execute script on
# our own origin otherwise, and our origin owns a root shell.
SANDBOX_INLINE = {".html", ".htm", ".xhtml", ".svg"}

# --------------------------------------------------------------- file handlers
#
# A handler is one thing that can be done with a file, attached to a group of
# extensions. It is the single source of truth for three things that used to
# live in three places (a hard-coded browser list, per-extension file_actions,
# and a visible_extensions list):
#
#   left click     the first handler, top to bottom, that lists the file's
#                  extension and has "click": true
#   right click    every handler that lists the extension, the left-click one
#                  first and bold, followed by the generic menu actions
#   folder panel   a file is shown by default when any handler lists its
#                  extension (folders, executables and the dotfile switch
#                  keep their own rules)
#
# kind:
#   browser   open the file in a new browser tab
#   editor    open the file in the built-in text editor
#   command   type `command` into the terminal
# execute: run immediately (true) or type it and wait for Enter (false).
# Only meaningful for command.

HANDLER_KINDS = ("browser", "editor", "command")

# Kinds that older versions shipped and this one no longer has. A handler of a
# retired kind in a saved settings.json is dropped quietly instead of making
# the whole handlers list invalid.
_RETIRED_HANDLER_KINDS = ("desktop",)

_TEXT_NATIVE = {".html", ".htm", ".xhtml", ".xml", ".json", ".txt", ".log", ".md"}
_MEDIA = sorted(ext for ext in BROWSER_NATIVE if ext not in _TEXT_NATIVE)
_TEXT = sorted(_TEXT_NATIVE | {
    ".js", ".ts", ".css", ".yaml", ".yml", ".toml", ".ini", ".conf", ".csv",
    ".env", ".sql", ".rs", ".go", ".c", ".h", ".cpp", ".java", ".rb", ".php",
    ".lua", ".py", ".sh",
})


def _handler(hid: str, name: str, icon: str, kind: str, extensions: list[str],
             command: str = "", execute: bool = False, click: bool = True) -> dict[str, Any]:
    return {"id": hid, "name": name, "icon": icon, "kind": kind,
            "extensions": sorted(set(extensions)), "command": command,
            "execute": execute, "click": click}


# Order matters: earlier handlers win the left click. .py and .sh are in the
# text editor group too, but the run handlers sit above it, so a click runs
# them and the editor is one right-click away.
DEFAULT_HANDLERS: list[dict[str, Any]] = [
    _handler("run_python", "Run with Python", "\u25b8", "command", [".py"],
             "python3 {file_path}"),
    _handler("run_bash", "Run with Bash", "\u25b8", "command", [".sh"],
             "bash {file_path}"),
    _handler("extract_zip", "Extract here", "\u2637", "command", [".zip"],
             "unzip {file_path} -d {cwd}"),
    _handler("extract_tar", "Extract here", "\u2637", "command",
             [".tar", ".gz", ".xz", ".bz2"], "tar -xf {file_path} -C {cwd}"),
    _handler("extract_7z", "Extract here", "\u2637", "command", [".7z", ".rar"],
             "7z x {file_path} -o{cwd}"),
    _handler("view_browser", "Open in browser", "\u25c9", "browser", _MEDIA),
    _handler("edit_text", "Edit as text", "\u270f", "editor", _TEXT),
    _handler("preview_html", "Preview in browser", "\u25c9", "browser",
             [".html", ".htm", ".xhtml"], click=False),
]

# ------------------------------------------------------------------ file icons
#
# The emoji in front of each name in the folder panel. Separate from handlers
# on purpose: a handler's icon marks an action in a menu (▸ run, ◉ open), and
# a file can have several handlers but only one look. First match wins:
# folder, then the first group listing the extension, then executable, then
# the default.

def _icon_group(gid: str, name: str, icon: str, extensions: list[str]) -> dict[str, Any]:
    return {"id": gid, "name": name, "icon": icon, "extensions": sorted(set(extensions))}


DEFAULT_FILE_ICONS: dict[str, Any] = {
    "folder": "\U0001F4C1",          # 📁
    "default": "\U0001F4C4",         # 📄
    "executable": "\u2699\ufe0f",    # ⚙️
    "groups": [
        _icon_group("images", "Images", "\U0001F5BC\ufe0f",
                    [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico",
                     ".avif", ".heic", ".tif", ".tiff", ".psd", ".xcf", ".raw"]),
        _icon_group("audio", "Audio", "\U0001F3B5",
                    [".mp3", ".wav", ".ogg", ".aac", ".m4a", ".flac", ".opus", ".wma"]),
        _icon_group("video", "Video", "\U0001F3AC",
                    [".mp4", ".webm", ".ogv", ".m4v", ".mkv", ".avi", ".mov", ".wmv",
                     ".flv", ".mpg", ".mpeg"]),
        _icon_group("archives", "Archives", "\U0001F4E6",
                    [".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".7z", ".rar", ".zst",
                     ".deb", ".rpm", ".iso", ".img"]),
        _icon_group("pdf", "PDF", "\U0001F4D5", [".pdf"]),
        _icon_group("python", "Python", "\U0001F40D", [".py", ".pyw", ".ipynb"]),
        _icon_group("scripts", "Scripts", "\U0001F4DC",
                    [".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat"]),
        _icon_group("code", "Code", "\U0001F9E9",
                    [".js", ".ts", ".jsx", ".tsx", ".css", ".html", ".htm", ".rs", ".go",
                     ".c", ".h", ".cpp", ".hpp", ".java", ".rb", ".php", ".lua", ".sql"]),
        _icon_group("config", "Config and data", "\U0001F527",
                    [".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".env",
                     ".xml", ".csv", ".service"]),
        _icon_group("text", "Text", "\U0001F4DD", [".txt", ".md", ".log", ".rst"]),
        _icon_group("documents", "Documents", "\U0001F4C3",
                    [".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt",
                     ".pptx", ".odp", ".epub"]),
    ],
}

MAX_ICON_LENGTH = 8      # one emoji can be several code points (ZWJ, variants)


def normalize_file_icons(raw: Any) -> dict[str, Any]:
    """Validate a file_icons value from the settings panel."""
    if not isinstance(raw, dict):
        raise SettingsError("file_icons has to be an object.")

    def icon(value: Any, fallback: str) -> str:
        text = str(value or "").strip()[:MAX_ICON_LENGTH]
        return text or fallback

    out = {key: icon(raw.get(key), DEFAULT_FILE_ICONS[key])
           for key in ("folder", "default", "executable")}
    groups = []
    for index, item in enumerate(raw.get("groups") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()[:40]
        if not name:
            raise SettingsError(f"Icon group {index + 1} has no name.")
        extensions = sorted({_norm_ext(str(e)) for e in item.get("extensions") or []
                             if str(e).strip().strip(".")})
        groups.append({
            "id": str(item.get("id") or f"g{index}")[:40],
            "name": name,
            "icon": icon(item.get("icon"), DEFAULT_FILE_ICONS["default"]),
            "extensions": extensions,
        })
    out["groups"] = groups
    return out


DEFAULT_SETTINGS: dict[str, Any] = {
    "shell": os.environ.get("SHELL") or "/bin/bash",
    "start_dir": str(Path.home()),
    "show_dotfiles": False,
    "show_executables": True,
    "handlers": DEFAULT_HANDLERS,
    "file_icons": DEFAULT_FILE_ICONS,
    # Command assistant: a command that bash cannot find is sent to the model
    # (Settings › Model), and its suggestions are printed in the terminal.
    "assist_on_not_found": True,
    # Send the model the current folder's listing and the last terminal
    # lines along with the request, so "delete these" has something to see.
    "assist_share_context": True,
    # Run each new shell inside Shellwalk's own tmux server, so it survives
    # Shellwalk restarting (Settings › General).
    "tmux_sessions": False,
    # The guided tour has been shown (or skipped) once. Kept here, not in the
    # browser, so it shows once per install rather than once per browser or
    # address (a tunnel URL that changes would otherwise bring it back).
    "tour_seen": False,
    # Phones: type into a real text box and send the line on Enter, instead
    # of key by key through xterm (Settings › General).
    "compose_box": True,
    # Injection behaviour
    "clear_line_before_inject": True,
    "block_inject_when_busy": True,
    # Terminal
    "scrollback_replay_bytes": 200_000,
    "font_size": 14,
    "cwd_poll_ms": 250,
    # Which address run.py binds to on its *next* start — read directly from
    # this file by run.py itself, before the app exists to serve anything.
    # Changing it from Settings writes here, then asks the app to restart
    # (see /api/network/bind); nothing about the running process rebinds.
    "bind_host": "127.0.0.1",
    # The port run.py listens on, read the same way (`run.py --port` wins).
    "port": 8022,
}

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


class SettingsError(ValueError):
    """A settings patch that cannot be saved as given."""


def _norm_ext(raw: str) -> str:
    ext = raw.strip().lower()
    return ext if ext.startswith(".") else f".{ext}"


def normalize_handlers(raw: Any) -> list[dict[str, Any]]:
    """Validate a handlers list coming from the UI or from disk."""
    if not isinstance(raw, list):
        raise SettingsError("handlers must be a list.")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SettingsError(f"Handler #{index + 1} is not an object.")
        name = str(item.get("name", "")).strip()
        kind = item.get("kind")
        if kind in _RETIRED_HANDLER_KINDS:
            continue
        if not name:
            raise SettingsError(f"Handler #{index + 1} has no name.")
        if kind not in HANDLER_KINDS:
            raise SettingsError(f"\u201c{name}\u201d has an unknown kind: {kind!r}.")
        command = str(item.get("command", "")).strip()
        if kind == "command" and not command:
            raise SettingsError(f"\u201c{name}\u201d needs a command.")
        extensions = sorted({_norm_ext(e) for e in item.get("extensions", [])
                             if isinstance(e, str) and e.strip().strip(".")})
        if not extensions:
            raise SettingsError(f"\u201c{name}\u201d lists no extensions.")
        hid = str(item.get("id") or "").strip() or f"handler_{index}"
        while hid in seen:
            hid += "_"
        seen.add(hid)
        result.append({
            "id": hid, "name": name, "icon": str(item.get("icon", "")).strip()[:3],
            "kind": kind, "extensions": extensions,
            "command": command if kind == "command" else "",
            "execute": bool(item.get("execute", False)),
            "click": bool(item.get("click", True)),
        })
    return result


def _migrate(stored: dict[str, Any]) -> bool:
    """Turn the old file_actions + visible_extensions pair into handlers.

    Runs once, the first time a settings.json from before handlers is read.
    Returns True when it changed `stored`, so the caller writes it back.
    """
    if "handlers" in stored or not ({"file_actions", "visible_extensions"} & stored.keys()):
        return False
    handlers = [dict(h, extensions=list(h["extensions"])) for h in DEFAULT_HANDLERS]

    def covered(ext: str) -> bool:
        return any(ext in h["extensions"] for h in handlers)

    # A customised click action becomes its own handler at the top, so it keeps
    # winning the left click exactly as before.
    # The click actions that shipped before handlers existed. Left as they
    # were, they are already covered by DEFAULT_HANDLERS and need no handler
    # of their own.
    shipped = {
        ".py": "python3 {file_path}", ".sh": "bash {file_path}",
        ".zip": "unzip {file_path} -d {cwd}", ".tar": "tar -xf {file_path} -C {cwd}",
        ".gz": "tar -xzf {file_path} -C {cwd}",
    }
    custom: list[dict[str, Any]] = []
    for ext, action in (stored.get("file_actions") or {}).items():
        if not isinstance(action, dict):
            continue
        ext = _norm_ext(ext)
        mode, command = action.get("mode"), (action.get("command") or "").strip()
        if mode in ("paste", "execute") and command:
            if shipped.get(ext) == command and mode == "paste":
                continue
            custom.append(_handler(f"custom{ext.replace('.', '_')}", f"Run ({ext})",
                                   "\u25b8", "command", [ext], command, mode == "execute"))
        elif mode == "view_text" and not covered(ext):
            handlers[[h["id"] for h in handlers].index("edit_text")]["extensions"].append(ext)

    # Extensions that were visible but have no handler are not carried over:
    # with nothing to open them, they go back to being hidden by default.

    stored["handlers"] = normalize_handlers(custom + handlers)
    stored.pop("file_actions", None)
    stored.pop("visible_extensions", None)
    return True


def _drop_retired_handlers(stored: dict[str, Any]) -> bool:
    """Remove saved handlers whose kind this version no longer has.

    Returns True when it changed `stored`, so the caller writes it back.
    """
    handlers = stored.get("handlers")
    if not isinstance(handlers, list):
        return False
    kept = [h for h in handlers
            if not (isinstance(h, dict) and h.get("kind") in _RETIRED_HANDLER_KINDS)]
    if len(kept) == len(handlers):
        return False
    stored["handlers"] = kept
    return True


def load_settings(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        data = dict(DEFAULT_SETTINGS)
        if SETTINGS_FILE.exists():
            try:
                stored = json.loads(SETTINGS_FILE.read_text())
                if isinstance(stored, dict):
                    # Both run; `|` rather than `or` so neither is skipped.
                    if _migrate(stored) | _drop_retired_handlers(stored):
                        SETTINGS_FILE.write_text(json.dumps(stored, indent=2))
                    data.update(stored)
            except (OSError, json.JSONDecodeError):
                pass
        _cache = data
        return data


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    global _cache
    if _cache is None:
        load_settings()      # merge into what is on disk, never into bare defaults
    if "handlers" in patch:
        patch = dict(patch, handlers=normalize_handlers(patch["handlers"]))
    if "file_icons" in patch:
        patch = dict(patch, file_icons=normalize_file_icons(patch["file_icons"]))
    with _lock:
        current = dict(_cache or DEFAULT_SETTINGS)
        current.update(patch)
        # Keys retired by handlers never come back through a stale client.
        current.pop("file_actions", None)
        current.pop("visible_extensions", None)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2))
        _cache = current
        return current


def handlers_for(ext: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Every handler listing `ext`, in configured order."""
    return [h for h in settings.get("handlers", []) if ext in h.get("extensions", [])]
