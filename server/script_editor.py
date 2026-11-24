"""Editing scripts (the topbar's right-hand side) from the interface.

A script is a folder containing a manifest.json and a routes.py, in one of
two layers: the shipped ones in the code directory's app/scripts/ (read-only
at runtime, replaced by updates — the Settings chip and its tabs live there),
and your own in DATA_HOME/scripts/. A folder in your layer with the same id as
a shipped one replaces it. Discovery already infers everything structural from what's on
disk — components._read_manifest() sets action to "route" the moment it sees
a routes.py sitting next to the manifest, with no field to set by hand. That
leaves exactly two things this module needs to do: write the display metadata
(name, description, icon, order, style, confirm dialog), and get the actual
Python onto disk. The second part is why this module exists rather than
sharing command_editor.py outright — a saved command's payload is a shell
string typed into a form; a script's payload is a file.

From the interface, a script is a script, whichever layer it is in: you can
edit, replace or delete any of them. What differs is only where the change is
written, because nothing may be written into the code directory:

  your own script     edits go straight into its folder; delete removes it
  a shipped script    metadata edits go to DATA_HOME/scripts/overrides.json,
                      laid on top at discovery (as context menus do); replacing
                      its code first copies the folder into your layer; delete
                      records {"removed": true} there, so the next update does
                      not quietly bring it back

Removing Settings removes the only chip that opens it — there is no hidden
fallback, and this module will not invent one. To get a removed shipped script
back, delete its entry from overrides.json. The confirm dialog shows the
script's own description before you commit, same as any other script.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from .config import SCRIPTS_DIR, USER_SCRIPTS_DIR

OVERRIDES_FILE = USER_SCRIPTS_DIR / "overrides.json"

EDITABLE: dict[str, type] = {
    "name": str,
    "description": str,
    "icon": str,
    "order": int,
    "enabled": bool,
    "style": str,
    "confirm": bool,
    "confirm_message": str,
}

STYLES = ("default", "info", "warning", "danger")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ScriptError(Exception):
    """An expected, blocking problem with an edit or an upload."""


def _in_layer(layer: Path, script_id: str) -> Path:
    if not ID_PATTERN.match(script_id or ""):
        raise ScriptError("That script id is not a valid folder name.")
    folder = (layer / script_id).resolve()
    try:
        folder.relative_to(layer.resolve())
    except ValueError:
        raise ScriptError("That path is outside the scripts folder.")
    return folder


def _folder(script_id: str) -> Path:
    """Always in your layer: the only place this module ever writes."""
    return _in_layer(USER_SCRIPTS_DIR, script_id)


def _shipped(script_id: str) -> Path | None:
    folder = _in_layer(SCRIPTS_DIR, script_id)
    return folder if (folder / "manifest.json").is_file() else None


# --- Overrides for shipped scripts --------------------------------------------

def load_overrides() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(OVERRIDES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _save_overrides(data: dict[str, dict[str, Any]]) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if data:
        OVERRIDES_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        OVERRIDES_FILE.unlink(missing_ok=True)


def _override(script_id: str, values: dict[str, Any]) -> None:
    data = load_overrides()
    data[script_id] = {**data.get(script_id, {}), **values}
    _save_overrides(data)


def _read_manifest(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptError(f"That script's manifest.json cannot be read: {exc}")
    return data if isinstance(data, dict) else {}


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
                raise ScriptError(f"'{name}' has to be a number.")
        else:
            cleaned[name] = str(value).strip()
    if "name" in cleaned and not cleaned["name"]:
        raise ScriptError("A script needs a name.")
    if cleaned.get("style") and cleaned["style"] not in STYLES:
        raise ScriptError(f"Style has to be one of {', '.join(STYLES)}.")
    return cleaned


def _unique_id(seed: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(seed).lower()).strip("_")[:40] or "script"
    if not base[0].isalnum():
        base = f"s_{base}".strip("_")
    candidate, counter = base, 2
    while (SCRIPTS_DIR / candidate).exists() or (USER_SCRIPTS_DIR / candidate).exists():
        candidate = f"{base}_{counter}"
        counter += 1
    if not ID_PATTERN.match(candidate):
        raise ScriptError("That name does not turn into a usable folder name.")
    return candidate


def _write_code(folder: Path, filename: str, data: bytes) -> None:
    """Put the uploaded code where discovery expects it: <folder>/routes.py.

    A .py upload is written as-is. A .zip is extracted — with the classic
    zip-slip check, since nothing about "the person uploaded this from their
    own machine" should be trusted to also mean "every path inside is safe to
    write" — and must contain routes.py at its top level; a script whose
    entry point discovery can't find is a script that silently does nothing,
    which is a worse failure than refusing the upload up front.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ScriptError("That file is larger than 10 MB.")

    lower = filename.lower()
    if lower.endswith(".py"):
        (folder / "routes.py").write_bytes(data)
        return

    if lower.endswith(".zip"):
        import io
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ScriptError("That doesn't look like a valid zip file.")
        root = folder.resolve()
        for member in archive.infolist():
            member_path = (folder / member.filename).resolve()
            try:
                member_path.relative_to(root)
            except ValueError:
                raise ScriptError(
                    f"Refusing to extract '{member.filename}' — it points outside "
                    "the script's own folder.")
        archive.extractall(folder)
        if not (folder / "routes.py").is_file():
            raise ScriptError(
                "The zip was extracted but there's no routes.py at its top level, "
                "so nothing would ever run. Nothing was kept.")
        return

    raise ScriptError("Upload a .py file (your routes.py) or a .zip containing one.")


def create(payload: dict[str, Any], filename: str, data: bytes) -> str:
    values = clean(payload)
    if not values.get("name"):
        raise ScriptError("Give the script a name.")
    if not filename or not data:
        raise ScriptError("Attach a routes.py or a zip containing one.")

    script_id = _unique_id(payload.get("id") or values["name"])
    folder = _folder(script_id)
    folder.mkdir(parents=True, exist_ok=False)
    try:
        _write_code(folder, filename, data)
        manifest = {
            "name": values["name"],
            "description": values.get("description", ""),
            "icon": values.get("icon") or "\u2731",
            "order": values.get("order", 200),
        }
        for optional in ("style", "confirm", "confirm_message", "enabled"):
            if optional in values:
                manifest[optional] = values[optional]
        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    except (ScriptError, OSError):
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return script_id


def patch(script_id: str, values: dict[str, Any]) -> None:
    """Change display metadata. For the code itself, see replace_code()."""
    folder = _folder(script_id)
    if not (folder / "manifest.json").is_file():
        if _shipped(script_id) is None:
            raise ScriptError("There is no such script.")
        cleaned = clean(values)
        if cleaned:
            _override(script_id, cleaned)
        return
    cleaned = clean(values)
    if not cleaned:
        return
    manifest = _read_manifest(folder)
    manifest.update(cleaned)
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def replace_code(script_id: str, filename: str, data: bytes) -> None:
    """Swap out an existing script's routes.py for a re-uploaded version."""
    folder = _folder(script_id)
    if not filename or not data:
        raise ScriptError("Attach a routes.py or a zip containing one.")
    if not (folder / "manifest.json").is_file():
        shipped = _shipped(script_id)
        if shipped is None:
            raise ScriptError("There is no such script.")
        # Copy on write: from here on this script is yours, in your layer.
        shutil.copytree(shipped, folder, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (folder / "routes.py").unlink(missing_ok=True)
    _write_code(folder, filename, data)


def remove(script_id: str) -> None:
    """Delete a script, whichever layer it is in (see the module docstring)."""
    folder = _folder(script_id)
    shipped = _shipped(script_id)
    if not (folder / "manifest.json").is_file() and shipped is None:
        raise ScriptError("There is no such script.")
    shutil.rmtree(folder, ignore_errors=True)
    if shipped is not None:
        _override(script_id, {"removed": True})
