"""Pluggable services: one active implementation per category.

Core code never imports a specific connector. It asks this loader for the
active service in a category and calls a small stable interface:

  LLM       async def ask(prompt: str, context: str = "") -> str

Adding a backend means adding services/<category>/<name>/ with a manifest.json
and a service.py. services/<category>/active.json names the one a new install
starts with; picking another in Settings is recorded in DATA_HOME, never in
services/ itself (see ACTIVE_FILE). No core file changes.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .config import DATA_DIR, SERVICES_DIR

# What the user chose in Settings (model name, address, ...), layered over each
# manifest's "config". Kept apart from the manifests so an update that ships a
# new manifest never wipes a choice, and a fresh install ships no choice at all.
USER_CONFIG_FILE = DATA_DIR / "service_config.json"
# Which service is active per category, as chosen in Settings: {"llm": "..."}.
# Kept out of services/ for the same reason — that folder is code, replaced by
# updates — and falling back to the shipped services/<category>/active.json
# for a category you never chose anything in.
ACTIVE_FILE = DATA_DIR / "service_active.json"
# Keys the interface may change. Anything else stays manifest-only.
EDITABLE_KEYS = ("base_url", "model")

CATEGORIES = ("llm",)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def user_config(category: str, service_id: str) -> dict[str, Any]:
    entry = _read_json(USER_CONFIG_FILE).get(f"{category}/{service_id}", {})
    return entry if isinstance(entry, dict) else {}


def effective_config(category: str, service_id: str) -> dict[str, Any]:
    manifest = _read_json(SERVICES_DIR / category / service_id / "manifest.json")
    config = dict(manifest.get("config", {}) or {})
    config.update(user_config(category, service_id))
    return config


def set_user_config(category: str, service_id: str, values: dict[str, Any]) -> None:
    if not (SERVICES_DIR / category / service_id / "manifest.json").is_file():
        raise ValueError("Unknown service.")
    data = _read_json(USER_CONFIG_FILE)
    key = f"{category}/{service_id}"
    entry = dict(data.get(key, {}) if isinstance(data.get(key), dict) else {})
    for name in EDITABLE_KEYS:
        if name in values:
            value = str(values[name] or "").strip()
            if value:
                entry[name] = value
            else:
                entry.pop(name, None)  # empty: fall back to the manifest / auto
    data[key] = entry
    USER_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_FILE.write_text(json.dumps(data, indent=2))
    if active_id(category) == service_id:
        _instances.pop(category, None)


def load_module(category: str, service_id: str):
    """Import a service's module (fresh, unconfigured instance)."""
    service_file = SERVICES_DIR / category / service_id / "service.py"
    if not service_file.exists():
        return None
    module_name = f"shellside_service_{category}_{service_id}"
    spec = importlib.util.spec_from_file_location(module_name, service_file)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def available(category: str) -> list[dict[str, Any]]:
    root = SERVICES_DIR / category
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        manifest = _read_json(folder / "manifest.json")
        if not manifest:
            continue
        manifest["id"] = folder.name
        manifest["dir"] = str(folder)
        manifest.setdefault("name", folder.name)
        manifest.setdefault("description", "")
        manifest.setdefault("config", {})
        manifest["config"] = {**manifest["config"], **user_config(category, folder.name)}
        manifest["editable"] = [k for k in EDITABLE_KEYS if k in manifest["config"]]
        out.append(manifest)
    return out


def active_id(category: str) -> str | None:
    chosen = _read_json(ACTIVE_FILE).get(category)
    # A choice naming a service an update has since removed falls back to the
    # shipped default rather than leaving the category with nothing.
    if isinstance(chosen, str) and (SERVICES_DIR / category / chosen / "manifest.json").is_file():
        return chosen
    pointer = _read_json(SERVICES_DIR / category / "active.json")
    return pointer.get("active")


def set_active(category: str, service_id: str) -> None:
    data = _read_json(ACTIVE_FILE)
    data[category] = service_id
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(json.dumps(data, indent=2))
    _instances.pop(category, None)


_instances: dict[str, Any] = {}


def get(category: str):
    """Return the active service module for a category, or None."""
    if category in _instances:
        return _instances[category]
    service_id = active_id(category)
    if not service_id:
        return None
    module = load_module(category, service_id)
    if module is None:
        return None
    if hasattr(module, "configure"):
        module.configure(effective_config(category, service_id))
    _instances[category] = module
    return module


def describe() -> dict[str, Any]:
    return {
        category: {
            "active": active_id(category),
            "available": available(category),
        }
        for category in CATEGORIES
    }
