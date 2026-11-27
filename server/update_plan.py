"""Update planning — pure logic, no FastAPI, no global state.

Turns "the bytes of a zip" into "a merge plan that has already been validated",
and nothing else. The routes in main.py only translate HTTP to and from these
functions.

**The merge is never applied from here.** This module only prepares
`_pending_update/`. Applying it is run.py's job, on the restart, before the
application's code is loaded again — see the lifecycle below and README.

    preview  →  _pending_update/payload/       imported content, as-is
                _pending_update/plan.json      the plan, NOT armed
    apply    →  _pending_update/manifest.json  ARMED: run.py looks for this
    restart  →  run.py applies it and deletes _pending_update/

Until `manifest.json` exists, everything in `_pending_update/` is inert
rubbish: an abandoned preview has no effect at all and is overwritten by the
next one.
"""
from __future__ import annotations

import json
import py_compile
import secrets
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

# --- Constants shared with run.py --------------------------------------------
# run.py does NOT import this module: it must be able to apply an update even
# when the update itself has broken server/. These names are duplicated there
# on purpose. They are a contract on disk, not shared code.
PENDING_DIRNAME = "_pending_update"
PAYLOAD_DIRNAME = "payload"
PLAN_FILENAME = "plan.json"          # preview, NOT armed
MANIFEST_FILENAME = "manifest.json"  # armed: run.py executes it on next start
PLAN_VERSION = 1

DELETE_FILENAME = "delete.json"

# The perimeter of the merge. Not an exclusion list: this is the only place
# looked in and the only place written to, by construction.
#
#   server/    the backend
#   web/       the frontend
#   app/       shipped folder-driven configuration and defaults (your own
#              lives in DATA_HOME, outside the perimeter: server/datahome.py)
#   services/  pluggable service implementations
MERGE_ROOTS = ("server", "web", "app", "services")

# Single FILES (not folders) allowed at the same anchor level as MERGE_ROOTS.
# Unlike the roots they are optional: an update is valid with only roots and
# none of these, and equally valid the other way round.
#
# run.py is on the list, so the launcher can update itself. It is the most
# delicate file here — if it arrives broken the machine does not come back, and
# on a headless box there is no fixing that remotely — so:
#   - .py files on this list are COMPILED here, at preview time, before they
#     can ever be armed. A syntax error fails the analysis, not the restart;
#   - run.py recompiles again before overwriting itself, keeps a run.py.bak,
#     and relaunches itself immediately afterwards;
#   - no MERGE_FILES entry can be DELETED (see _validate_delete_target): a
#     delete.json can never leave the project without its launcher.
MERGE_FILES = ("README.md", "run.py", "requirements.txt")

# Noise not worth carrying in an update (matches what the backup zip skips).
SKIP_DIRS = {"__pycache__", ".git", "node_modules", PENDING_DIRNAME}
SKIP_SUFFIXES = {".pyc", ".pyo"}

# Where user state lived inside a merge root before it moved to DATA_HOME.
# Nothing reads these any more except the one-time copy into DATA_HOME, so an
# update that carries one is almost certainly a stale backup of someone's own
# settings rather than code, and worth a warning.
USER_STATE = ("app/settings.json", "app/context_menu/overrides.json")


class UpdateError(Exception):
    """An expected, BLOCKING error while preparing an update."""


# --- Working paths -----------------------------------------------------------

def pending_dir(base: Path) -> Path:
    return base / PENDING_DIRNAME


def payload_dir(base: Path) -> Path:
    return pending_dir(base) / PAYLOAD_DIRNAME


def clear_pending(base: Path) -> None:
    """Delete any pending preparation, previewed or armed."""
    shutil.rmtree(pending_dir(base), ignore_errors=True)


def pending_status(base: Path) -> dict:
    folder = pending_dir(base)
    status = {
        "staged": (folder / PAYLOAD_DIRNAME).is_dir(),
        "armed": (folder / MANIFEST_FILENAME).is_file(),
    }
    plan_file = folder / PLAN_FILENAME
    if plan_file.is_file():
        try:
            plan = json.loads(plan_file.read_text())
            status["plan"] = {k: plan.get(k) for k in
                              ("token", "created", "source", "counts")}
        except (OSError, json.JSONDecodeError):
            pass
    return status


# --- Staging: get the incoming content onto disk -----------------------------

def _safe_relpath(raw: str) -> str | None:
    """Reject absolute paths and anything climbing out with '..'."""
    candidate = raw.replace("\\", "/").strip()
    if not candidate or candidate.startswith("/"):
        return None
    parts = [p for p in candidate.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if len(parts) > 1 and ":" in parts[0]:      # C:/... on Windows-made zips
        return None
    return "/".join(parts) if parts else None


def stage_zip(base: Path, raw: bytes, zip_basename: str) -> dict:
    """Unpack an uploaded zip into _pending_update/payload/.

    Any previous preparation is discarded first: only one update can be in
    flight at a time, and a half-finished older one must never survive.
    """
    clear_pending(base)
    payload = payload_dir(base)
    payload.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    try:
        tmp.write_bytes(raw)
        try:
            archive = zipfile.ZipFile(tmp)
        except zipfile.BadZipFile:
            raise UpdateError("That file is not a readable zip archive.")
        with archive:
            extracted = 0
            for member in archive.infolist():
                if member.is_dir():
                    continue
                relative = _safe_relpath(member.filename)
                if relative is None:
                    continue
                parts = Path(relative).parts
                if any(p in SKIP_DIRS for p in parts):
                    continue
                if Path(relative).suffix.lower() in SKIP_SUFFIXES:
                    continue
                destination = payload / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                extracted += 1
        if not extracted:
            raise UpdateError("The zip contained no usable files.")
    finally:
        tmp.unlink(missing_ok=True)

    stem = Path(zip_basename).stem
    staging = {"source": zip_basename, "hint": stem, "stripped_wrapper": None}

    # Shortcut: a zip named after a root IS the content of that root, even if
    # it happens to contain a folder of that name as well.
    if stem in MERGE_ROOTS:
        inner = payload / stem
        if inner.is_dir():
            staging["stripped_wrapper"] = stem
    return staging


# --- Reading the payload -----------------------------------------------------

def _is_noise(parts) -> bool:
    if any(p in SKIP_DIRS for p in parts):
        return True
    return bool(parts) and Path(parts[-1]).suffix.lower() in SKIP_SUFFIXES


def _level_entries(level: Path) -> tuple[list[Path], list[Path]]:
    """Directories and files directly inside `level`, noise excluded."""
    directories, files = [], []
    if not level.is_dir():
        return directories, files
    for entry in sorted(level.iterdir()):
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if entry.is_dir():
            directories.append(entry)
        elif entry.suffix.lower() not in SKIP_SUFFIXES:
            files.append(entry)
    return directories, files


def _descend_double(root: Path) -> Path:
    """server/server/... -> use the inner one. The real project has no such nesting."""
    inner = root / root.name
    return inner if inner.is_dir() else root


def resolve_roots(payload: Path, hint: str) -> dict:
    """Find where the update hooks onto the project.

    An update always starts from one of MERGE_ROOTS. The path inside the zip
    *is* the destination path — no searching by name, no ambiguity to resolve.

    Rules, in order:
      1. roots present at this level -> anchor here
      2. the zip is named after a root -> this level is that root's content
      3. exactly one folder and nothing else -> anonymous wrapper, descend
    """
    containers: list[str] = []
    level = payload

    for _ in range(12):
        directories, files = _level_entries(level)
        names = {d.name for d in directories}

        # Rule 1: real roots found here.
        found_roots = {n: _descend_double(level / n) for n in MERGE_ROOTS if n in names}
        found_files = {f.name: f for f in files if f.name in MERGE_FILES}

        if found_roots or found_files:
            ignored = [d.name for d in directories if d.name not in MERGE_ROOTS]
            ignored += [f.name for f in files if f.name not in MERGE_FILES
                        and f.name != DELETE_FILENAME]
            return {"anchor": level, "roots": found_roots, "files": found_files,
                    "containers": containers, "ignored": sorted(ignored)}

        # Rule 2: the zip is named after a root, so this level is its content.
        if hint in MERGE_ROOTS and (directories or files):
            return {"anchor": level, "roots": {hint: level}, "files": {},
                    "containers": containers, "ignored": []}

        # Rule 3: one lone folder and nothing else -> anonymous wrapper.
        if len(directories) == 1 and not files:
            containers.append(directories[0].name)
            level = directories[0]
            continue

        break

    raise UpdateError(
        "I cannot find where this update starts. The zip needs "
        f"{', '.join(f'{r}/' for r in MERGE_ROOTS)} at the top level, with the "
        "project's own structure inside — or it needs to be named after one of "
        "them, e.g. server.zip."
    )


def _read_delete_json(payload: Path, anchor: Path) -> tuple[list[str], list[str]]:
    """delete.json sits at the anchor level, beside the roots."""
    warnings: list[str] = []
    for candidate in (anchor / DELETE_FILENAME, payload / DELETE_FILENAME):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"{DELETE_FILENAME} could not be read ({exc}): ignored.")
            return [], warnings
        entries = data.get("delete") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            warnings.append(f"{DELETE_FILENAME} has no 'delete' list: ignored.")
            return [], warnings
        return [str(e) for e in entries if isinstance(e, str)], warnings
    return [], warnings


# --- Validation --------------------------------------------------------------

def _validate_delete_target(base: Path, raw: str) -> tuple[Path | None, str | None]:
    """A delete entry must be a full relative path, inside a root, below it."""
    relative = _safe_relpath(raw)
    if relative is None:
        return None, f"'{raw}' is not a valid relative path: ignored."
    parts = Path(relative).parts
    if parts[0] not in MERGE_ROOTS:
        return None, (f"'{raw}' is outside the perimeter "
                      f"({', '.join(MERGE_ROOTS)}): ignored.")
    if len(parts) < 2:
        return None, f"'{raw}' would delete an entire root: refused."
    return base / relative, None


def _valida_py(source: Path, name: str) -> None:
    """Compile an incoming .py before it can ever be armed."""
    try:
        py_compile.compile(str(source), cfile=str(source) + ".check",
                           doraise=True, quiet=1)
    except py_compile.PyCompileError as exc:
        raise UpdateError(
            f"{name} in this update does not compile, so it was not armed:\n{exc}")
    finally:
        Path(str(source) + ".check").unlink(missing_ok=True)


def _add_copy(copy: list, seen: dict, payload: Path, base: Path,
              source: Path, destination: Path, action: str) -> None:
    destination_rel = destination.relative_to(base).as_posix()
    source_rel = source.relative_to(payload).as_posix()
    if destination_rel in seen:
        raise UpdateError(
            f"Two parts of this update write to the same file: "
            f"'{seen[destination_rel]}' and '{source_rel}' both become "
            f"'{destination_rel}'. Keep only one.")
    seen[destination_rel] = source_rel
    copy.append({"from": source_rel, "to": destination_rel, "action": action,
                 "size": source.stat().st_size})


# --- The plan ----------------------------------------------------------------

def build_plan(base: Path, staging: dict) -> dict:
    """Compute the full plan from content already in _pending_update/payload/.

    Writes nothing inside the merge roots: this only decides.
    """
    payload = payload_dir(base)
    if not payload.is_dir() or not any(payload.iterdir()):
        raise UpdateError("The imported content is empty.")

    found = resolve_roots(payload, staging.get("hint", ""))

    hooks: list[dict] = []
    copy: list[dict] = []
    seen: dict[str, str] = {}

    for name in MERGE_ROOTS:                     # stable order
        content = found["roots"].get(name)
        if content is None:
            continue
        hooks.append({"root": name,
                      "from": content.relative_to(payload).as_posix() or "."})
        for source in sorted(content.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(content)
            if _is_noise(relative.parts):
                continue
            if relative.as_posix() == DELETE_FILENAME:
                continue                          # an instruction, never content
            destination = base / name / relative
            _add_copy(copy, seen, payload, base, source, destination,
                      "overwrite" if destination.exists() else "add")

    for name in MERGE_FILES:
        source = found["files"].get(name)
        if source is None:
            continue
        if name.endswith(".py"):
            _valida_py(source, name)              # blocking
        hooks.append({"root": name,
                      "from": source.relative_to(payload).as_posix()})
        destination = base / name
        _add_copy(copy, seen, payload, base, source, destination,
                  "overwrite" if destination.exists() else "add")

    raw_deletes, warnings = _read_delete_json(payload, found["anchor"])
    deletes: list[str] = []
    for raw in raw_deletes:
        target, warning = _validate_delete_target(base, raw)
        if warning:
            warnings.append(warning)
            continue
        relative = target.relative_to(base).as_posix()
        if relative not in deletes:
            deletes.append(relative)

    if not copy and not deletes:
        raise UpdateError(
            "This update has nothing to do: no files inside "
            f"{', '.join(r + '/' for r in MERGE_ROOTS)} or "
            f"{', '.join(MERGE_FILES)}, and no valid deletions.")

    if staging.get("stripped_wrapper"):
        warnings.append(f"Dropped the wrapper '{staging['stripped_wrapper']}/': "
                        "it is named after the zip.")
    for container in found["containers"]:
        warnings.append(f"Dropped the wrapper '{container}/': it held a single folder.")
    for name in found["ignored"]:
        warnings.append(f"'{name}' is outside the perimeter: ignored, nothing will "
                        "touch it.")
    for entry in copy:
        if entry["to"] in USER_STATE:
            warnings.append(f"This update overwrites '{entry['to']}', which holds "
                            "your own settings. That is usually a mistake.")

    plan = {
        "version": PLAN_VERSION,
        "token": secrets.token_hex(8),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": staging.get("source", ""),
        "hooks": hooks,
        "copy": copy,
        "delete": deletes,
        "warnings": warnings,
    }
    plan["counts"] = {
        "overwrite": sum(1 for c in copy if c["action"] == "overwrite"),
        "add": sum(1 for c in copy if c["action"] == "add"),
        "delete": len(deletes),
    }
    plan["trees"] = build_trees(base, plan)
    return plan


# --- Preview trees -----------------------------------------------------------

def _child(node: dict, name: str) -> dict:
    for existing in node["children"]:
        if existing["name"] == name:
            return existing
    fresh = {"name": name, "action": None, "dir": True, "children": []}
    node["children"].append(fresh)
    return fresh


def _insert(roots: dict, base: Path, relative: str, action: str, is_dir: bool) -> None:
    parts = Path(relative).parts
    root_name = parts[0]
    if root_name not in roots:
        roots[root_name] = {"name": root_name, "action": None, "dir": True,
                            "children": []}
    node = roots[root_name]
    for part in parts[1:-1]:
        node = _child(node, part)
    if len(parts) > 1:
        node = _child(node, parts[-1])
    node["action"] = action
    node["dir"] = is_dir


def _sort_tree(node: dict) -> None:
    node["children"].sort(key=lambda n: (not n["dir"], n["name"].lower()))
    for child in node["children"]:
        _sort_tree(child)


def build_trees(base: Path, plan: dict) -> list[dict]:
    roots: dict[str, dict] = {}
    for entry in plan["copy"]:
        _insert(roots, base, entry["to"], entry["action"], False)
    for relative in plan["delete"]:
        _insert(roots, base, relative, "delete", (base / relative).is_dir())
    trees = list(roots.values())
    for tree in trees:
        _sort_tree(tree)
    trees.sort(key=lambda t: t["name"].lower())
    return trees


# --- Writing and arming ------------------------------------------------------

def write_plan(base: Path, plan: dict) -> None:
    (pending_dir(base) / PLAN_FILENAME).write_text(json.dumps(plan, indent=2))


def arm(base: Path, token: str) -> dict:
    """Promote a previewed plan to an armed manifest that run.py will execute.

    The token ties the confirmation to the exact preview the user looked at: if
    a newer preview has replaced it, arming fails rather than applying
    something they never saw.
    """
    plan_file = pending_dir(base) / PLAN_FILENAME
    if not plan_file.is_file():
        raise UpdateError("There is no prepared update to apply.")
    try:
        plan = json.loads(plan_file.read_text())
    except (OSError, json.JSONDecodeError):
        raise UpdateError("The prepared update is unreadable. Import it again.")
    if plan.get("token") != token:
        raise UpdateError("This preview is out of date. Import the update again.")
    (pending_dir(base) / MANIFEST_FILENAME).write_text(json.dumps(plan, indent=2))
    return plan
