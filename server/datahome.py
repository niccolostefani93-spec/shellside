"""Where user state lives, and how it gets there the first time.

The code directory is meant to be something you can `git pull` into, publish,
or throw away and re-clone. That only works if nothing the app writes at
runtime lives inside it. So all user state — settings, saved commands,
favorites, service choices, your own menu actions and scripts, credentials,
the supervisor's pid file — lives under one directory, DATA_HOME:

    $SHELLSIDE_HOME                       if set (tests, a second instance)
    $XDG_CONFIG_HOME/shellside            otherwise
    ~/.config/shellside                   when XDG_CONFIG_HOME is unset

What stays in the code directory under app/ is only what ships: the default
commands/favorites/data that seed a new DATA_HOME, and the settings panel and
context menu, which are read straight from the code so an update reaches you.

Standard library only, and no imports from the rest of server/: run.py imports
this before it has checked that the app's own dependencies are installed, and
it must not drag in anything that could fail for that reason. run.py also
keeps its own tiny copy of resolve_data_home() for the case where server/
cannot be imported at all (a broken update); tests/test_core.py checks that
the two copies agree.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def resolve_data_home() -> Path:
    """The user-data root. Keep in step with run.py's _data_home()."""
    explicit = os.environ.get("SHELLSIDE_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser().absolute()
    return default_data_home()


def default_data_home() -> Path:
    """Where DATA_HOME is when SHELLSIDE_HOME is not set."""
    # The XDG spec says a relative XDG_CONFIG_HOME is invalid and must be
    # ignored, rather than resolved against whatever the cwd happens to be.
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return base / "shellside"


# Entries that may exist in DATA_HOME before anything worth keeping is in it.
# run.py writes its pid file under run/ even if it could not import this module
# to migrate first; that alone must not make a fresh DATA_HOME look "already
# set up" and skip the legacy migration for good.
_NOT_USER_STATE = {"run"}


def _is_fresh(home: Path) -> bool:
    if not home.exists():
        return True
    try:
        return all(entry.name in _NOT_USER_STATE for entry in home.iterdir())
    except OSError:
        return False


# --------------------------------------------------------- legacy migration
#
# Until this module existed, user state lived inside the code directory:
#
#   .auth/                         credentials (secret_key, auth.json, devices)
#   app/settings.json              settings
#   app/commands/*.json            saved commands (all of them yours to edit)
#   app/favorites/paths.json       favorites
#   app/data/                      service_config.json and your own notes
#   app/context_menu/overrides.json   edits to shipped menu actions
#   app/context_menu/<s>/<id>/     menu actions you made ("user": true)
#   services/<category>/active.json   which service is active
#
# It is COPIED, never moved: the old files stay exactly where they were, so
# going back to the previous version of the code still finds them, and a
# migration that went wrong half way has destroyed nothing.

def _legacy_user_menu_actions(root: Path) -> list[Path]:
    """Menu action folders created from the interface, in the old location."""
    found: list[Path] = []
    menus = root / "app" / "context_menu"
    if not menus.is_dir():
        return found
    for manifest in sorted(menus.glob("*/*/manifest.json")):
        try:
            if json.loads(manifest.read_text()).get("user"):
                found.append(manifest.parent)
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return found


def legacy_state_present(root: Path) -> bool:
    """True when the code directory holds state only a running install creates.

    Deliberately not triggered by app/commands, app/favorites or app/data: a
    fresh clone ships those as defaults, and copying them is seeding, not
    migrating. Only things no clone ever contains count as proof that this
    directory has been used.
    """
    return ((root / ".auth").is_dir()
            or (root / "app" / "settings.json").is_file()
            or (root / "app" / "context_menu" / "overrides.json").is_file()
            or bool(_legacy_user_menu_actions(root)))


def _copy_tree(source: Path, destination: Path) -> None:
    # copy2 keeps mode bits and times, which is what keeps .auth/'s 0600 files
    # 0600 on the other side.
    shutil.copytree(source, destination, copy_function=shutil.copy2,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def migrate_legacy(root: Path, home: Path) -> bool:
    """Copy pre-DATA_HOME state out of the code directory, once.

    Runs only while DATA_HOME is still fresh, so it can never overwrite
    anything: once there is any user state in DATA_HOME it is the source of
    truth and the old copy is ignored for good. Built in a staging folder next
    to DATA_HOME and renamed into place, so an interrupted copy leaves no
    half-migrated DATA_HOME behind to be mistaken for a finished one.
    Returns True when it migrated something.
    """
    if not _is_fresh(home) or not legacy_state_present(root):
        return False

    home.parent.mkdir(parents=True, exist_ok=True)
    staging = home.parent / f".{home.name}.migrating-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        app = root / "app"
        if (root / ".auth").is_dir():
            _copy_tree(root / ".auth", staging / "auth")
            os.chmod(staging / "auth", 0o700)
        if (app / "settings.json").is_file():
            shutil.copy2(app / "settings.json", staging / "settings.json")
        for name in ("commands", "favorites", "data"):
            if (app / name).is_dir():
                _copy_tree(app / name, staging / name)
        if (app / "context_menu" / "overrides.json").is_file():
            (staging / "context_menu").mkdir(exist_ok=True)
            shutil.copy2(app / "context_menu" / "overrides.json",
                         staging / "context_menu" / "overrides.json")
        for folder in _legacy_user_menu_actions(root):
            _copy_tree(folder, staging / "context_menu" / folder.parent.name / folder.name)

        # Which service is active used to be written into services/ itself.
        active: dict[str, str] = {}
        services = root / "services"
        if services.is_dir():
            for pointer in sorted(services.glob("*/active.json")):
                try:
                    value = json.loads(pointer.read_text()).get("active")
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(value, str) and value:
                    active[pointer.parent.name] = value
        if active:
            (staging / "data").mkdir(exist_ok=True)
            (staging / "data" / "service_active.json").write_text(
                json.dumps(active, indent=2) + "\n")

        if home.exists():
            # Only run/ can be in it (see _is_fresh); move ours in beside it.
            for entry in staging.iterdir():
                entry.rename(home / entry.name)
            staging.rmdir()
        else:
            staging.rename(home)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return True


# ------------------------------------------------------------------ seeding
#
# What ships in app/ and becomes yours to change. Each is copied only when it
# is missing, never over something that exists:
#
#   commands/    only when the folder itself is missing. Deleting a command
#                deletes it (see command_editor.py), so re-adding missing
#                files on every start would bring back what you removed.
#   favorites/   per file
#   data/        per file — a deleted service_config.json coming back empty
#                is harmless, and new reference notes reach existing installs
#
# settings.json is never seeded: the defaults live in config.DEFAULT_SETTINGS
# and only what you change is written, so a new default reaches you too.

def seed_defaults(root: Path, home: Path) -> list[str]:
    """Copy shipped defaults that DATA_HOME lacks. Returns what was added."""
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    shipped = root / "app"
    added: list[str] = []

    if (shipped / "commands").is_dir() and not (home / "commands").exists():
        _copy_tree(shipped / "commands", home / "commands")
        added.append("commands/")

    for name in ("favorites", "data"):
        source_root = shipped / name
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts:
                continue
            relative = source.relative_to(shipped)
            target = home / relative
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            added.append(relative.as_posix())
    return added


def prepare(root: Path, home: Path) -> None:
    """Migrate (once), then seed. Safe to call any number of times.

    Called from run.py before anything else touches DATA_HOME, and again from
    server/config.py at import, for when the app is started without run.py.
    The second call finds nothing to do.
    """
    if migrate_legacy(root, home):
        print(f"  Shellside: copied your settings and credentials from {root} to "
              f"{home} (the originals were left in place).", flush=True)
    seed_defaults(root, home)
