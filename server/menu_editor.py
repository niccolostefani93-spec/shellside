"""Editing context menus from the interface, without losing the folder model.

Everything in a menu is still a folder with a manifest.json, in one of two
layers, and this module only adds two ways to change one:

  a shipped action   its folder is in the code directory (app/context_menu/),
                     which is read-only at runtime and replaced by updates.
                     Edits are therefore NOT written into it. They go to
                     DATA_HOME/context_menu/overrides.json, which is yours,
                     survives updates, and is applied on top at discovery time.

  your own action    created here, in DATA_HOME/context_menu/<menu>/<id>/, so
                     the folder itself is yours. Edits are written straight
                     into its manifest.json and command.txt, because nothing
                     upstream will ever fight you for them.

Which of the two applies is decided by where the folder is: an action in the
DATA_HOME layer is yours (it also carries "user": true, as it always has).
A folder there with the same id as a shipped one replaces it. Only your own
actions can be deleted; a shipped action can be hidden ("enabled": false) but
not removed, so an update can always put back a working menu.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .config import CONTEXT_MENU_DIR, USER_CONTEXT_MENU_DIR

OVERRIDES_FILE = USER_CONTEXT_MENU_DIR / "overrides.json"

# Menus a new action can be attached to. Not discovered from disk: adding a
# surface means teaching the frontend where to open it, which is a code change.
SURFACES = ("file_entry", "command_line", "log")

# What the interface is allowed to change. Anything else in a manifest —
# action, builtin, needs_picker, prompt — is structure, not preference, and
# stays where it is written.
EDITABLE: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "description": str,
    "icon": str,
    "order": int,
    "enabled": bool,
    "execute": bool,          # run on click, or type it and wait for Enter
    "confirm": bool,
    "confirm_message": str,
    "style": str,
    "bold": bool,
    "scope": str,
    "quote": bool,
    "command": str,
    "match": dict,
}

STYLES = ("default", "danger", "warning", "info", "accent")
SCOPES = ("entry", "folder", "any")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")


class MenuError(Exception):
    """An expected, blocking problem with an edit."""


# --- Overrides file ----------------------------------------------------------

def load_overrides() -> dict[str, dict[str, Any]]:
    if not OVERRIDES_FILE.is_file():
        return {}
    try:
        data = json.loads(OVERRIDES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict) and "/" in k}


def save_overrides(data: dict[str, dict[str, Any]]) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if data:
        OVERRIDES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        OVERRIDES_FILE.unlink(missing_ok=True)


def key(surface: str, component_id: str) -> str:
    return f"{surface}/{component_id}"


# --- Locating a component folder ---------------------------------------------

def _in_layer(layer: Path, surface: str, component_id: str) -> Path:
    if surface not in SURFACES:
        raise MenuError(f"Unknown menu '{surface}'.")
    if not ID_PATTERN.match(component_id or ""):
        raise MenuError("That action id is not a valid folder name.")
    folder = (layer / surface / component_id).resolve()
    try:
        folder.relative_to(layer.resolve())
    except ValueError:
        raise MenuError("That path is outside the context menu folder.")
    return folder


def _user_folder(surface: str, component_id: str) -> Path:
    return _in_layer(USER_CONTEXT_MENU_DIR, surface, component_id)


def _folder(surface: str, component_id: str) -> Path:
    """The folder discovery actually uses: yours if there is one, else shipped."""
    mine = _user_folder(surface, component_id)
    if (mine / "manifest.json").is_file():
        return mine
    return _in_layer(CONTEXT_MENU_DIR, surface, component_id)


def _read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MenuError(f"That action's manifest.json cannot be read: {exc}")
    return data if isinstance(data, dict) else {}


def is_user_owned(surface: str, component_id: str) -> bool:
    try:
        folder = _user_folder(surface, component_id)
    except MenuError:
        return False
    return (folder / "manifest.json").is_file()


# --- Validation --------------------------------------------------------------

def clean(values: dict[str, Any]) -> dict[str, Any]:
    """Keep the editable keys, drop the rest, and check the types."""
    cleaned: dict[str, Any] = {}
    for name, value in (values or {}).items():
        expected = EDITABLE.get(name)
        if expected is None:
            continue
        if expected is bool:
            cleaned[name] = bool(value)
        elif expected is int:
            try:
                cleaned[name] = int(value)
            except (TypeError, ValueError):
                raise MenuError(f"'{name}' has to be a number.")
        elif expected is str:
            cleaned[name] = str(value).strip()
        elif expected is dict:
            cleaned[name] = value if isinstance(value, dict) else {}
    if cleaned.get("style") and cleaned["style"] not in STYLES:
        raise MenuError(f"Style has to be one of {', '.join(STYLES)}.")
    if cleaned.get("scope") and cleaned["scope"] not in SCOPES:
        raise MenuError(f"Scope has to be one of {', '.join(SCOPES)}.")
    if "match" in cleaned:
        cleaned["match"] = _clean_match(cleaned["match"])
    if "command" in cleaned and not cleaned["command"]:
        raise MenuError("An action needs a command.")
    if "name" in cleaned and not cleaned["name"]:
        raise MenuError("An action needs a name.")
    return cleaned


def _clean_match(raw: dict[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {}
    extensions = raw.get("extensions")
    if isinstance(extensions, list):
        cleaned = []
        for item in extensions:
            text = str(item).strip().lower()
            if not text:
                continue
            cleaned.append(text if text.startswith(".") else f".{text}")
        if cleaned:
            match["extensions"] = sorted(set(cleaned))
    kinds = raw.get("kinds")
    if isinstance(kinds, list):
        cleaned = [k for k in kinds if k in ("file", "dir")]
        if cleaned and len(cleaned) < 2:
            match["kinds"] = cleaned
    return match


# --- The four operations -----------------------------------------------------

def patch(surface: str, component_id: str, values: dict[str, Any]) -> None:
    """Change an action, writing wherever that action's edits belong."""
    folder = _folder(surface, component_id)
    if not (folder / "manifest.json").is_file():
        raise MenuError("There is no such action.")
    cleaned = clean(values)
    if not cleaned:
        return

    if is_user_owned(surface, component_id):
        manifest = _read_manifest(folder)
        command = cleaned.pop("command", None)
        manifest.update(cleaned)
        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        if command is not None:
            (folder / "command.txt").write_text(command.strip() + "\n")
        return

    overrides = load_overrides()
    entry = dict(overrides.get(key(surface, component_id), {}))
    entry.update(cleaned)
    overrides[key(surface, component_id)] = entry
    save_overrides(overrides)


def reset(surface: str, component_id: str) -> None:
    """Forget every override for a shipped action; it goes back to as-shipped."""
    overrides = load_overrides()
    if overrides.pop(key(surface, component_id), None) is not None:
        save_overrides(overrides)


def create(payload: dict[str, Any]) -> str:
    """Make a new action folder. Returns its id.

    Only command actions can be made this way. A builtin needs a handler in the
    frontend and a route needs a routes.py — neither is something a form can
    conjure, and pretending otherwise would create menu entries that do nothing.
    """
    surface = payload.get("surface", "")
    if surface not in SURFACES:
        raise MenuError(f"Unknown menu '{surface}'.")

    values = clean(payload)
    name = values.get("name", "")
    command = values.get("command", "")
    if not name:
        raise MenuError("Give the action a name.")
    if not command:
        raise MenuError("Give the action a command to run.")

    component_id = _unique_id(surface, payload.get("id") or name)
    folder = _user_folder(surface, component_id)
    folder.mkdir(parents=True, exist_ok=False)

    manifest = {
        "name": name,
        "description": values.get("description", ""),
        "icon": values.get("icon") or "\u2731",
        "order": values.get("order", 200),
        "action": "command",
        "execute": values.get("execute", False),
        "user": True,
    }
    for optional in ("style", "bold", "confirm", "confirm_message", "scope",
                     "quote", "match", "enabled"):
        if optional in values:
            manifest[optional] = values[optional]
    if manifest.get("match") == {}:
        manifest.pop("match")

    try:
        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        (folder / "command.txt").write_text(command.strip() + "\n")
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise MenuError(f"Could not write the action: {exc}")
    return component_id


def _unique_id(surface: str, seed: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(seed).lower()).strip("_")[:40]
    if not base or not base[0].isalnum():
        base = f"action_{base}".strip("_")
    base = base[:40] or "action"
    candidate = base
    counter = 2
    while ((CONTEXT_MENU_DIR / surface / candidate).exists()
           or (USER_CONTEXT_MENU_DIR / surface / candidate).exists()):
        candidate = f"{base}_{counter}"
        counter += 1
    if not ID_PATTERN.match(candidate):
        raise MenuError("That name does not turn into a usable folder name.")
    return candidate


def remove(surface: str, component_id: str) -> None:
    """Delete an action you made. Shipped ones can only be hidden."""
    if not is_user_owned(surface, component_id):
        raise MenuError(
            "That action came with the project, so it cannot be deleted \u2014 "
            "an update would only put it back. Hide it instead.")
    folder = _user_folder(surface, component_id)
    shutil.rmtree(folder, ignore_errors=True)
    reset(surface, component_id)
