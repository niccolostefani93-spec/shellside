"""Filesystem-as-configuration discovery.

A component is any folder containing a manifest.json. It may also carry:

  command.txt   a single templated shell command
  routes.py     a module exposing `invoke(ctx) -> dict` for anything that needs
                real logic

The top bar, the settings tabs and every context menu are built by walking their
folders. Adding a feature is adding a folder; nothing here holds a registry that
has to be edited in step.

Scripts and context menus are walked in two layers: the shipped one in the code
directory (app/scripts, app/context_menu), then yours in DATA_HOME (scripts/,
context_menu/). A folder in your layer replaces the shipped one with the same
id; anything else from either layer is listed. Saved commands live only in
DATA_HOME/commands (see server/datahome.py for how a new install gets them).

Discovery results are cached and invalidated by a cheap recursive mtime scan, so
edits on disk show up without a restart, and without a filesystem-watch
dependency that would need its own thread and platform handling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .config import (COMMANDS_DIR, CONTEXT_MENU_DIR, SCRIPTS_DIR,
                     USER_CONTEXT_MENU_DIR, USER_SCRIPTS_DIR)

# ----------------------------------------------------------------- manifests

DEFAULT_MANIFEST: dict[str, Any] = {
    "name": None,          # falls back to folder name
    "description": "",
    "icon": "",
    "order": 100,
    "enabled": True,
    "style": "default",    # default | danger | warning | info | accent
    "bold": False,         # default actions are bold (doc section 3.5)
    "confirm": False,
    "confirm_message": "",
    "quote": True,
    "execute": False,      # run immediately vs paste and wait for Enter
    "match": {},           # {"extensions": [...], "kinds": ["file"|"dir"]}
    # Where in a menu an entry applies. Only the folder panel distinguishes
    # them, and only since the empty-area menu was folded into this one:
    #   entry   right-clicked a file or folder (the default)
    #   folder  right-clicked the panel background or the breadcrumb
    #   any     both
    "scope": "entry",
    # Optional {"field": ..., "title": ..., "label": ..., "default": ...}. The
    # frontend asks for that one value before the command is rendered, so an
    # action can need a name without needing a routes.py.
    "prompt": None,
}


def _read_manifest(folder: Path) -> dict[str, Any] | None:
    manifest_file = folder / "manifest.json"
    if not manifest_file.exists():
        return None
    try:
        raw = json.loads(manifest_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {**DEFAULT_MANIFEST, "id": folder.name, "name": folder.name,
                "broken": f"manifest.json could not be read: {exc}"}
    manifest = {**DEFAULT_MANIFEST, **(raw if isinstance(raw, dict) else {})}
    manifest["id"] = folder.name
    manifest["name"] = manifest.get("name") or folder.name.replace("_", " ").title()
    manifest["dir"] = str(folder)
    command_file = folder / "command.txt"
    if command_file.exists():
        manifest["command"] = command_file.read_text().strip()
    manifest["has_routes"] = (folder / "routes.py").exists()
    if "action" not in manifest:
        if manifest.get("command"):
            manifest["action"] = "command"
        elif manifest["has_routes"]:
            manifest["action"] = "route"
        else:
            manifest["action"] = "builtin"
    return manifest


def _walk(root: Path, depth: int = 0, max_depth: int = 6) -> list[dict[str, Any]]:
    """Collect components under root, preserving nesting for settings sub-tabs."""
    found: list[dict[str, Any]] = []
    if not root.is_dir() or depth > max_depth:
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "__")):
            continue
        manifest = _read_manifest(child)
        if manifest is None:
            # A plain grouping folder: descend anyway.
            found.extend(_walk(child, depth + 1, max_depth))
            continue
        children_root = child / "tabs" if (child / "tabs").is_dir() else child
        nested = _walk(children_root, depth + 1, max_depth)
        if nested:
            manifest["children"] = nested
        if manifest.get("enabled", True):
            found.append(manifest)
    found.sort(key=lambda m: (m.get("order", 100), m["name"].lower()))
    return found


def _walk_layers(shipped: Path, mine: Path) -> list[dict[str, Any]]:
    """Both layers' top-level components, yours replacing shipped by id."""
    merged = {item["id"]: item for item in _walk(shipped)}
    merged.update({item["id"]: item for item in _walk(mine)})
    found = list(merged.values())
    found.sort(key=lambda m: (m.get("order", 100), m["name"].lower()))
    return found


# ------------------------------------------------------------------- caching

_cache: dict[str, Any] = {}
_signature: int | None = None


def _tree_signature() -> int:
    total = 0
    for root in (SCRIPTS_DIR, COMMANDS_DIR, CONTEXT_MENU_DIR,
                 USER_SCRIPTS_DIR, USER_CONTEXT_MENU_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            try:
                total ^= hash((str(path), int(path.stat().st_mtime_ns)))
            except OSError:
                continue
    return total


def invalidate() -> None:
    global _signature, _cache
    _signature = None
    _cache = {}


def discover(force: bool = False) -> dict[str, Any]:
    global _signature, _cache
    signature = _tree_signature()
    if _cache and signature == _signature and not force:
        return _cache

    from . import menu_editor, script_editor   # here: both import config only

    overrides = menu_editor.load_overrides()
    context_menus: dict[str, list[dict[str, Any]]] = {}
    surfaces = sorted({surface.name
                       for layer in (CONTEXT_MENU_DIR, USER_CONTEXT_MENU_DIR)
                       if layer.is_dir()
                       for surface in layer.iterdir()
                       if surface.is_dir() and not surface.name.startswith(".")})
    for name in surfaces:
        items = _walk_layers(CONTEXT_MENU_DIR / name, USER_CONTEXT_MENU_DIR / name)
        _apply_overrides(name, items, overrides)
        context_menus[name] = items
    # An override can change "order", so sorting has to happen after it.
    for items in context_menus.values():
        items.sort(key=lambda m: (m.get("order", 100), m["name"].lower()))

    _cache = {
        "scripts": _scripts(script_editor.load_overrides()),
        "commands": load_commands(),
        "context_menus": context_menus,
    }
    _signature = signature
    return _cache


def _scripts(overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Scripts from both layers, with your edits to shipped ones laid on top.

    Overrides only ever name shipped scripts (script_editor writes your own
    edits into your own folders), so one that a folder of yours now replaces
    is simply ignored. {"removed": true} drops the shipped script entirely.
    """
    items = _walk_layers(SCRIPTS_DIR, USER_SCRIPTS_DIR)
    result = []
    for item in items:
        patch = overrides.get(item["id"])
        if patch and not (USER_SCRIPTS_DIR / item["id"]).is_dir():
            if patch.get("removed"):
                continue
            item.update(patch)
            if not item.get("enabled", True):
                continue
        result.append(item)
    result.sort(key=lambda m: (m.get("order", 100), m["name"].lower()))
    return result


def _apply_overrides(surface: str, items: list[dict[str, Any]],
                     overrides: dict[str, dict[str, Any]]) -> None:
    """Lay DATA_HOME/context_menu/overrides.json on top of what the folders said.

    Shipped manifests are never edited in place: an update would overwrite the
    change without saying so. The overrides file is user state, so a preference
    like "Move should paste, not run" outlives every future import.

    Items disabled this way are kept in the list, not dropped, so the settings
    editor can still show them and offer to switch them back on. The menu
    builder in the frontend is what skips enabled:false.
    """
    for item in items:
        patch = overrides.get(f"{surface}/{item['id']}")
        if patch:
            item.update(patch)
            item["overridden"] = sorted(patch)
        _apply_overrides(surface, item.get("children", []), overrides)


def load_commands() -> list[dict[str, Any]]:
    """Saved commands: flat JSON files, one per command (doc section 6.2).

    One file, one command, no second place an edit might be hiding. See
    server/command_editor.py for how the interface writes these.
    """
    commands: list[dict[str, Any]] = []
    if not COMMANDS_DIR.is_dir():
        return commands
    for file in sorted(COMMANDS_DIR.glob("*.json")):
        try:
            data = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "command" not in data:
            continue
        entry = {
            "id": file.stem,
            "name": data.get("name") or file.stem,
            "description": data.get("description", ""),
            "command": data["command"],
            "order": data.get("order", 100),
            "topbar": data.get("topbar", True),
            "execute": data.get("execute", False),
            "quote": data.get("quote", True),
            "enabled": data.get("enabled", True),
            "confirm": data.get("confirm", False),
            "confirm_message": data.get("confirm_message", ""),
            "style": data.get("style", "default"),
        }
        commands.append(entry)
    commands.sort(key=lambda c: (c["order"], c["name"].lower()))
    return commands


def find(surface: str, component_id: str) -> dict[str, Any] | None:
    tree = discover()
    pool = (tree["scripts"] if surface == "scripts"
            else tree["context_menus"].get(surface, []))

    def search(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in items:
            if item["id"] == component_id:
                return item
            hit = search(item.get("children", []))
            if hit:
                return hit
        return None

    return search(pool)


# ------------------------------------------------------------- routes.py

def load_route(component: dict[str, Any]):
    """Import a component's routes.py and return its `invoke` callable.

    Modules are given a stable dotted name derived from their path so repeated
    loads replace rather than accumulate, and so two components can both define
    `invoke` without colliding.
    """
    folder = Path(component["dir"])
    route_file = folder / "routes.py"
    if not route_file.exists():
        return None
    module_name = "shellwalk_component_" + str(folder).strip("/").replace("/", "_").replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, route_file)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return getattr(module, "invoke", None)
