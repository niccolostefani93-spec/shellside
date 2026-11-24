"""Editing saved commands from the interface.

Every saved command is one flat JSON file in DATA_HOME/commands/, full stop.
There used to be a second class of command here — the ones that shipped with
the project, edited through an overrides.json instead of their own file,
deletable only as a "hide". That distinction added a file, a migration
concern, and two code paths for what is otherwise one idea: a name, a command
line, and a few display flags. It is gone. A command you delete is deleted,
whether you made it five minutes ago or it came in the box.

The commands in the code directory's app/commands/ are only the examples a new
install starts with: they are copied into DATA_HOME/commands/ once, when that
folder does not exist yet (server/datahome.py), and never again. So a command
you removed stays removed across updates — and, the other side of the same
choice, a new example shipped by an update does not appear in an existing
install by itself.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import COMMANDS_DIR

EDITABLE: dict[str, type] = {
    "name": str,
    "description": str,
    "command": str,
    "order": int,
    "topbar": bool,          # a chip in the top bar, or only in the list
    "execute": bool,         # run on click, or type it and wait for Enter
    "quote": bool,
    "enabled": bool,         # hidden from the top bar and the list, not deleted
    "confirm": bool,         # ask before running — for the ones that matter
    "confirm_message": str,
    "style": str,            # default | info | warning | danger — chip colour
}

STYLES = ("default", "info", "warning", "danger")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")

# Superseded by the file itself the moment this module loads — see
# migrate_legacy_overrides() below, called once at server startup.
_LEGACY_OVERRIDES_NAME = "overrides.json"


class CommandError(Exception):
    """An expected, blocking problem with an edit."""


def _file(command_id: str) -> Path:
    if not ID_PATTERN.match(command_id or ""):
        raise CommandError("That command id is not a valid file name.")
    target = (COMMANDS_DIR / f"{command_id}.json").resolve()
    try:
        target.relative_to(COMMANDS_DIR.resolve())
    except ValueError:
        raise CommandError("That path is outside the commands folder.")
    return target


def _read(command_id: str) -> dict[str, Any]:
    target = _file(command_id)
    if not target.is_file():
        raise CommandError("There is no such saved command.")
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"That command's file cannot be read: {exc}")
    return data if isinstance(data, dict) else {}


def _write(command_id: str, record: dict[str, Any]) -> None:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    _file(command_id).write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def clean(values: dict[str, Any]) -> dict[str, Any]:
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
                raise CommandError(f"'{name}' has to be a number.")
        else:
            cleaned[name] = str(value).strip()
    if "name" in cleaned and not cleaned["name"]:
        raise CommandError("A saved command needs a name.")
    if "command" in cleaned and not cleaned["command"]:
        raise CommandError("A saved command needs a command.")
    if cleaned.get("style") and cleaned["style"] not in STYLES:
        raise CommandError(f"Style has to be one of {', '.join(STYLES)}.")
    return cleaned


def _unique_id(seed: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(seed).lower()).strip("_")[:40] or "command"
    if not base[0].isalnum():
        base = f"c_{base}".strip("_")
    candidate, counter = base, 2
    while (COMMANDS_DIR / f"{candidate}.json").exists():
        candidate = f"{base}_{counter}"
        counter += 1
    if not ID_PATTERN.match(candidate):
        raise CommandError("That name does not turn into a usable file name.")
    return candidate


def create(payload: dict[str, Any]) -> str:
    values = clean(payload)
    if not values.get("name"):
        raise CommandError("Give the command a name.")
    if not values.get("command"):
        raise CommandError("Give it something to run.")

    command_id = _unique_id(payload.get("id") or values["name"])
    record = {
        "name": values["name"],
        "description": values.get("description", ""),
        "command": values["command"],
        "order": values.get("order", 200),
        "topbar": values.get("topbar", True),
        "execute": values.get("execute", False),
    }
    for optional in ("quote", "confirm", "confirm_message", "style"):
        if optional in values:
            record[optional] = values[optional]
    try:
        _write(command_id, record)
    except OSError as exc:
        raise CommandError(f"Could not write the command: {exc}")
    return command_id


def patch(command_id: str, values: dict[str, Any]) -> None:
    cleaned = clean(values)
    if not cleaned:
        return
    record = _read(command_id)
    record.update(cleaned)
    _write(command_id, record)


def remove(command_id: str) -> None:
    """Delete a saved command outright. Every command, regardless of origin."""
    _read(command_id)  # exists?
    _file(command_id).unlink(missing_ok=True)


def migrate_legacy_overrides() -> None:
    """One-time cleanup, run at startup: fold overrides.json into real files.

    Before this module dropped the shipped/user split, an edit to a command
    that came with the project was kept in commands/overrides.json instead
    of the file itself. That file, if it still exists, holds edits nobody
    should lose just because the distinction it served is gone. Apply each
    patch directly into its target and delete the override file. Safe to call
    on every startup — a second run finds nothing to do.
    """
    legacy = COMMANDS_DIR / _LEGACY_OVERRIDES_NAME
    if not legacy.is_file():
        return
    try:
        overrides = json.loads(legacy.read_text())
    except (OSError, json.JSONDecodeError):
        legacy.unlink(missing_ok=True)
        return
    if isinstance(overrides, dict):
        for command_id, patch_values in overrides.items():
            if not isinstance(patch_values, dict):
                continue
            try:
                record = _read(command_id)
            except CommandError:
                continue  # the command itself is gone; nothing to fold in
            record.update({k: v for k, v in patch_values.items() if k in EDITABLE})
            _write(command_id, record)
    legacy.unlink(missing_ok=True)
