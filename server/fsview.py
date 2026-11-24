"""Directory listings read straight from the filesystem.

Nothing in this module ever touches the PTY. Running `ls` in the shell to
populate the folder panel would spam the visible log and spawn a process on
every navigation (handoff doc section 2.1); os.scandir costs a syscall.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config import BROWSER_NATIVE, handlers_for, load_settings


def _is_executable(st: os.stat_result) -> bool:
    return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def classify(name: str, path: str, st: os.stat_result, is_dir: bool,
             is_link: bool, settings: dict[str, Any]) -> dict[str, Any]:
    ext = Path(name).suffix.lower()
    executable = (not is_dir) and _is_executable(st)
    native = ext in BROWSER_NATIVE
    handled = (not is_dir) and bool(handlers_for(ext, settings))

    # Dotfiles are checked before directories on purpose: .git, .cache and
    # .venv are directories, and letting them through would clutter the panel
    # in exactly the folders you navigate most.
    if name.startswith(".") and not settings.get("show_dotfiles", False):
        default_visible = False
    elif is_dir:
        default_visible = True
    elif handled:
        # One rule: if a click can do something with it, it is shown. There is
        # no separate list of visible extensions to keep in step.
        default_visible = True
    elif executable and settings.get("show_executables", True):
        default_visible = True
    else:
        default_visible = False

    return {
        "name": name,
        "path": path,
        "ext": ext,
        "dir": is_dir,
        "link": is_link,
        "executable": executable,
        "native": native,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "default_visible": default_visible,
    }


def list_dir(path: str, show_all: bool = False) -> dict[str, Any]:
    settings = load_settings()
    target = Path(path).expanduser()
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"path": str(target), "error": "That directory no longer exists.",
                "entries": [], "hidden_count": 0}
    if not target.is_dir():
        return {"path": str(target), "error": "That path is not a directory.",
                "entries": [], "hidden_count": 0}

    entries: list[dict[str, Any]] = []
    hidden = 0
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=True)
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        is_dir = False
                    except OSError:
                        continue
                info = classify(entry.name, str(target / entry.name), st,
                                is_dir, entry.is_symlink(), settings)
                if not info["default_visible"] and not show_all:
                    hidden += 1
                    continue
                entries.append(info)
    except PermissionError:
        return {"path": str(target), "error": "Permission denied.",
                "entries": [], "hidden_count": 0}

    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {
        "path": str(target),
        "parent": str(target.parent) if str(target) != "/" else None,
        "segments": breadcrumb(str(target)),
        "entries": entries,
        "hidden_count": hidden,
        "error": None,
    }


def breadcrumb(path: str) -> list[dict[str, str]]:
    """Absolute path split into clickable segments."""
    parts = Path(path).parts
    segments: list[dict[str, str]] = []
    accumulated = ""
    for part in parts:
        if part == "/":
            accumulated = "/"
            segments.append({"label": "/", "path": "/"})
            continue
        accumulated = os.path.join(accumulated, part)
        segments.append({"label": part, "path": accumulated})
    return segments


def mime_for(path: str) -> str:
    return BROWSER_NATIVE.get(Path(path).suffix.lower(), "application/octet-stream")
