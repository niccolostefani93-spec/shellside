"""Shellwalk backend."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import (FastAPI, File, Form, HTTPException, Query, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import (StreamingResponse, FileResponse, HTMLResponse, JSONResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import (assist, auth, auth_routes, command_editor, components, fsview,
               menu_editor, script_editor, services, update_plan)
from .gate import GateMiddleware
from .config import (BROWSER_NATIVE, DEFAULT_FILE_ICONS, DEFAULT_HANDLERS, DATA_DIR, DATA_HOME, FAVORITES_FILE,
                     ROOT, VERSION, SANDBOX_INLINE, WEB_DIR, SettingsError, load_settings,
                     save_settings)
from .pty_session import (TmuxViewer, registry, tmux_available, tmux_has, tmux_history,
                          tmux_sessions)
from .templating import render

app = FastAPI(title="Shellwalk", docs_url=None, redoc_url=None)
app.include_router(auth_routes.router)


@app.on_event("startup")
async def _rotate_local_token() -> None:
    """A fresh proof-of-locality token every boot — see server/auth.py.

    Printed here, not in run.py: this way it rotates on every restart,
    including one triggered by an applied update, without run.py needing to
    import anything from server/.
    """
    token = auth.new_local_token()
    port = os.environ.get("SHELLWALK_PORT", "8022")
    import sys
    if sys.stdout.isatty():
        print(f"\n  \033[38;5;179mShellwalk {VERSION}\033[0m is up. Signed out? This link "
              f"signs you back in from this machine, no password:")
        print(f"      http://127.0.0.1:{port}/auth/local?token={token}\n", flush=True)
    else:
        # A service: stdout goes to the journal, and a secret has no place in
        # a log. The owner reads the link with ./shellwalk link instead.
        print(f"Shellwalk {VERSION} is up on port {port}. "
              f"Sign-in link for this machine: ./shellwalk link", flush=True)


@app.on_event("startup")
async def _migrate_command_overrides() -> None:
    """Fold any pre-existing commands/overrides.json into real files.

    One-time, and safe to run every boot: once the file is gone this is a
    no-op forever after. See command_editor.migrate_legacy_overrides().
    """
    command_editor.migrate_legacy_overrides()


# --------------------------------------------------------------- security hook

# ------------------------------------------------------------------- terminal

@app.websocket("/ws/terminal")
async def terminal_socket(ws: WebSocket, session: str | None = Query(None)) -> None:
    await ws.accept()
    settings = load_settings()
    loop = asyncio.get_running_loop()

    pty_session = registry.get(session)
    resumed = pty_session is not None
    if pty_session is None:
        shell_options = dict(
            shell=settings["shell"],
            cwd=settings["start_dir"],
            scrollback_bytes=settings.get("scrollback_replay_bytes", 200_000),
        )
        # A tab whose shell outlived a Shellwalk restart in tmux gets that
        # same shell back, under the same id, instead of a fresh one.
        pty_session = registry.adopt(session, **shell_options)
        resumed = pty_session is not None
        if pty_session is None and session:
            # The tab asked for a shell that has ended: closed on another
            # device, exited, or lost to a restart. Its tab goes too, rather
            # than quietly turning into a new shell nobody asked for.
            await ws.send_json({"type": "closed", "id": session})
            await ws.close(1000)
            return
        if pty_session is None:
            pty_session = registry.create(
                **shell_options, use_tmux=bool(settings.get("tmux_sessions", False)))

    outbox: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_output(data: bytes) -> None:
        loop.call_soon_threadsafe(outbox.put_nowait, data)

    def on_closed() -> None:
        loop.call_soon_threadsafe(outbox.put_nowait, None)

    # The browser reports its size as soon as the socket opens. Wait briefly
    # for it, so a tmux client starts at the right size instead of 80x24.
    rows, cols = 24, 80
    early: list[dict] = []
    try:
        first = await asyncio.wait_for(ws.receive(), timeout=1.5)
        early.append(first)
        parsed = json.loads(first.get("text") or "null")
        if isinstance(parsed, dict) and parsed.get("type") == "resize":
            rows, cols = int(parsed.get("rows", 24)), int(parsed.get("cols", 80))
            early.clear()
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, TypeError):
        pass
    except (WebSocketDisconnect, RuntimeError):
        return

    # Shells in tmux: this browser gets its own tmux client, at its own size
    # (see TmuxViewer). Otherwise it shares the shell's one PTY.
    viewer: TmuxViewer | None = None
    pty_session.add_close_callback(on_closed)
    pty_session.attach_to_loop(loop)     # the shell's own reader: assistant, cwd, liveness
    await ws.send_json({"type": "session", "id": pty_session.id, "resumed": resumed,
                        "title": pty_session.title})
    if pty_session.tmux:
        history = tmux_history(pty_session.id)
        if history:
            # Then push it all above the screen, which tmux redraws on attach.
            await ws.send_bytes(history + b"\r\n" * rows)
        viewer = TmuxViewer(pty_session.id, rows, cols)
        pty_session.resize(rows, cols)
        viewer.start(loop, on_output, on_exit=lambda: on_output(b""))
    else:
        pty_session.add_listener(on_output)
        pty_session.client_resize(ws, rows, cols)
        if resumed:
            history = pty_session.scrollback()
            if history:
                await ws.send_bytes(history)

    size = [rows, cols]

    def handle(parsed: dict) -> None:
        kind = parsed.get("type")
        if kind == "input":
            data = parsed.get("data", "").encode()
            write_raw(data)
        elif kind == "resize":
            r, c = int(parsed.get("rows", 24)), int(parsed.get("cols", 80))
            size[0], size[1] = r, c
            if viewer is not None:
                viewer.resize(r, c)
            else:
                pty_session.client_resize(ws, r, c)

    def write_raw(data: bytes) -> None:
        if viewer is not None:
            # The shell's own tmux client takes the size of the device typing,
            # so text the app types into it (a panel action) does not pull
            # the pane to another device's size.
            pty_session.resize(*size)
            viewer.write(data)
        else:
            pty_session.client_input(ws)
            pty_session.write(data)

    async def pump_out() -> None:
        while True:
            data = await outbox.get()
            if data is None:            # its tab was closed, maybe elsewhere
                await ws.send_json({"type": "closed", "id": pty_session.id})
                await ws.close(1000)
                return
            if data == b"":             # our tmux client ended
                if tmux_has(pty_session.id):
                    await ws.close(1011)    # the browser reconnects
                else:
                    await ws.send_bytes(b"\r\n[shellwalk] shell exited\r\n")
                return
            await ws.send_bytes(data)

    def dispatch(message: dict) -> None:
        if (payload := message.get("bytes")) is not None:
            write_raw(payload)
        elif (text := message.get("text")) is not None:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                write_raw(text.encode())
                return
            if isinstance(parsed, dict):
                handle(parsed)

    pump = asyncio.create_task(pump_out())
    try:
        for message in early:
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            dispatch(message)
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            dispatch(message)
    except (WebSocketDisconnect, RuntimeError):     # RuntimeError: pump closed it
        pass
    finally:
        pump.cancel()
        if viewer is not None:
            viewer.close()
        else:
            pty_session.remove_listener(on_output)
            pty_session.client_gone(ws)
        pty_session.remove_close_callback(on_closed)
        # The shell survives a closed socket or browser: it is still one of
        # the open tabs (see /api/workspace). Only closing its tab ends it.


# -------------------------------------------------------------------- control

@app.websocket("/ws/control")
async def control_socket(ws: WebSocket, session: str | None = Query(None)) -> None:
    await ws.accept()
    settings = load_settings()
    poll_delay = max(0.05, settings.get("cwd_poll_ms", 250) / 1000)
    show_all = False
    last_cwd: str | None = None
    last_signature: tuple | None = None

    async def push_listing(path: str, force: bool = False) -> None:
        nonlocal last_signature
        listing = fsview.list_dir(path, show_all)
        # What counts as "the same listing". Names, kinds and the hidden count,
        # deliberately NOT mtime or size: a log file being written in the cwd
        # would otherwise re-render the panel several times a second.
        signature = (listing["path"], show_all, listing.get("error"),
                     listing.get("hidden_count"),
                     tuple((e["name"], e["dir"]) for e in listing["entries"]))
        if signature == last_signature and not force:
            return
        last_signature = signature
        await ws.send_json({"type": "listing", **listing})

    # Something happened that could have changed the folder. The rescan task
    # below waits on this instead of the filesystem being read on a timer.
    dirty = asyncio.Event()
    attached: object = None

    def on_output(_data: bytes) -> None:
        # Called from the PTY reader, on this same event loop. Any output at
        # all is a candidate: `touch x` prints nothing but still redraws a
        # prompt, and a script writing files prints as it goes.
        dirty.set()

    async def rescan() -> None:
        """Re-read the folder when something says it might have changed.

        The debounce is what makes this cheap. A command that prints a thousand
        lines sets `dirty` a thousand times and costs exactly one scandir,
        150ms after it goes quiet.
        """
        loop = asyncio.get_running_loop()
        while True:
            await dirty.wait()
            # Wait for the output to go quiet for 150ms. A program that never
            # stops printing (Claude Code's spinner, a progress bar, tail -f)
            # would otherwise cost a folder read every 150ms for as long as it
            # runs; with it, the folder is read at most every 2 seconds.
            deadline = loop.time() + 2.0
            while True:
                dirty.clear()
                await asyncio.sleep(0.15)
                if not dirty.is_set() or loop.time() >= deadline:
                    break
            dirty.clear()
            target = last_cwd or load_settings()["start_dir"]
            try:
                await push_listing(target)
            except (WebSocketDisconnect, RuntimeError):
                return

    async def watch() -> None:
        """Poll what the kernel will not tell us: who owns the terminal.

        There is no notification for tcgetpgrp() or for /proc/<pid>/cwd, so
        the status line has to ask. This loop asks, and never touches the
        filesystem — when it sees the shell come back to its prompt, it sets
        `dirty` and the rescan task does the reading.
        """
        nonlocal last_cwd, attached
        was_idle = True
        while True:
            pty_session = registry.get(session)
            if pty_session is not None:
                if pty_session is not attached:
                    if attached is not None:
                        attached.remove_listener(on_output)
                    pty_session.add_listener(on_output)
                    attached = pty_session
                    dirty.set()
                cwd = pty_session.cwd()
                if cwd and cwd != last_cwd:
                    last_cwd = cwd
                    await push_listing(cwd, force=True)
                idle = pty_session.is_idle()
                if idle and not was_idle:
                    # A command just finished. This is the moment that matters:
                    # rm, mv, mkdir, an unzip, a build that wrote a folder.
                    dirty.set()
                was_idle = idle
                await ws.send_json({
                    "type": "status",
                    "idle": idle,
                    "running": pty_session.foreground_command(),
                    "alive": pty_session.alive,
                })
            await asyncio.sleep(poll_delay)

    watcher = asyncio.create_task(watch())
    rescanner = asyncio.create_task(rescan())
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "attach":
                session = message.get("session") or session
                last_cwd = None
            elif kind == "show_all":
                show_all = bool(message.get("value"))
                target = last_cwd or load_settings()["start_dir"]
                await push_listing(target, force=True)
            elif kind == "peek":
                # Folder-picker modal browses without moving the shell.
                await ws.send_json({
                    "type": "peek",
                    **fsview.list_dir(message.get("path", "/"), show_all),
                })
            elif kind == "refresh":
                await push_listing(last_cwd or load_settings()["start_dir"], force=True)
            elif kind == "touched":
                # The frontend just did something to this folder (an upload, a
                # menu action it ran itself). Same path as a shell command.
                dirty.set()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        watcher.cancel()
        rescanner.cancel()
        if attached is not None:
            attached.remove_listener(on_output)


# ------------------------------------------------------------------ bootstrap

@app.get("/api/bootstrap")
async def bootstrap(request: Request) -> JSONResponse:
    tree = components.discover()
    settings = load_settings()
    return JSONResponse({
        "version": VERSION,
        "settings": settings,
        "default_handlers": DEFAULT_HANDLERS,
        "default_file_icons": DEFAULT_FILE_ICONS,
        "scripts": tree["scripts"],
        "commands": tree["commands"],
        "context_menus": tree["context_menus"],
        "favorites": load_favorites(),
        "services": services.describe(),
    })


@app.post("/api/reload")
async def reload_components() -> dict[str, Any]:
    components.invalidate()
    load_settings(force=True)
    return {"ok": True, **components.discover(force=True)}


# ------------------------------------------------------------------ listings

@app.get("/api/dir")
async def api_dir(path: str, show_all: bool = False) -> dict[str, Any]:
    return fsview.list_dir(path, show_all)


# --------------------------------------------------------------------- files

def _resolve(path: str) -> Path:
    target = Path(path).expanduser()
    try:
        return target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "No such file.")


@app.get("/api/file")
async def api_file(path: str) -> Response:
    target = _resolve(path)
    if not target.is_file():
        raise HTTPException(400, "Not a file.")
    ext = target.suffix.lower()
    # A browser handler can be pointed at any extension, so fall back to the
    # system's MIME table before giving up and calling it text.
    media_type = (BROWSER_NATIVE.get(ext) or mimetypes.guess_type(target.name)[0]
                  or "text/plain; charset=utf-8")
    headers = {"Content-Disposition": f'inline; filename="{target.name}"'}
    if ext in SANDBOX_INLINE:
        # This origin can open a root shell. Never let a previewed .html or
        # .svg run script against it.
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data: blob:"
    return FileResponse(target, media_type=media_type, headers=headers)


@app.put("/api/file")
async def api_file_write(path: str, request: Request) -> dict[str, Any]:
    """Backs web/edit.html's Save button. Writes the whole body over the
    existing file's contents — no diffing, no locking, same "last write
    wins" trade-off as everything else here. Only ever reaches an existing
    file: _resolve() requires one, on purpose — creating new files already
    has its own context-menu action."""
    target = _resolve(path)
    if not target.is_file():
        raise HTTPException(400, "Not a file.")
    body = await request.body()
    if len(body) > 5_000_000:
        raise HTTPException(413, "That file is over 5MB — too big to edit here.")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "Not valid UTF-8 text.")
    target.write_text(text)
    return {"ok": True, "size": len(body)}


@app.get("/api/download")
async def api_download(path: str) -> Response:
    """A file as it is, or a folder as a zip built while it downloads."""
    target = _resolve(path)
    if target.is_dir():
        name = (target.name or "root") + ".zip"
        return StreamingResponse(
            _zip_stream(target), media_type="application/zip",
            headers={"Content-Disposition": _attachment(name)})
    if not target.is_file():
        raise HTTPException(400, "Not a file.")
    return FileResponse(target, filename=target.name,
                        media_type="application/octet-stream")


def _attachment(name: str) -> str:
    from urllib.parse import quote
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}'


# Already compressed: deflating them again costs CPU and saves nothing.
_STORED_SUFFIXES = {
    ".zip", ".gz", ".tgz", ".xz", ".bz2", ".7z", ".rar", ".zst", ".jpg", ".jpeg",
    ".png", ".gif", ".webp", ".avif", ".heic", ".mp3", ".m4a", ".aac", ".ogg",
    ".opus", ".flac", ".mp4", ".mkv", ".mov", ".webm", ".avi", ".pdf", ".docx",
    ".xlsx", ".pptx", ".odt", ".ods", ".epub", ".jar", ".whl", ".deb",
    ".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx",
}


class _ZipSink:
    """A write-only file object for zipfile that hands its bytes to a generator."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.offset = 0
        self.pending = 0

    def write(self, data) -> int:
        self.chunks.append(bytes(data))
        self.offset += len(data)
        self.pending += len(data)
        return len(data)

    def tell(self) -> int:
        return self.offset

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self.chunks)
        self.chunks.clear()
        self.pending = 0
        return data


def _zip_stream(folder: Path):
    """Yield a zip of `folder` piece by piece, never holding a whole file.

    zipfile writes to a non-seekable sink in streaming mode (sizes go in a
    data descriptor after each file), so the download starts at once and a
    folder of any size never touches the disk or fills memory. A plain
    generator: Starlette runs it in a worker thread, off the event loop.
    Symlinks are stored as what they point to only when that stays inside
    the folder; unreadable files are skipped rather than ending the zip.
    """
    sink = _ZipSink()
    root = folder.resolve()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=1,
                         allowZip64=True) as archive:
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            base = Path(directory)
            relative_dir = base.relative_to(root)
            if not filenames and not dirnames and str(relative_dir) != ".":
                archive.writestr(f"{root.name}/{relative_dir.as_posix()}/", b"")
            for filename in sorted(filenames):
                path = base / filename
                try:
                    real = path.resolve(strict=True)
                    if not real.is_file() or not real.is_relative_to(root):
                        continue
                    info = zipfile.ZipInfo.from_file(
                        path, f"{root.name}/{(relative_dir / filename).as_posix()}")
                    info.compress_type = (zipfile.ZIP_STORED
                                          if path.suffix.lower() in _STORED_SUFFIXES
                                          else zipfile.ZIP_DEFLATED)
                    with path.open("rb") as source, \
                            archive.open(info, "w", force_zip64=True) as dest:
                        while chunk := source.read(1 << 20):
                            dest.write(chunk)
                            if sink.pending >= 1 << 20:
                                yield sink.take()
                except OSError:
                    continue
                if sink.chunks:
                    yield sink.take()
    yield sink.take()


@app.post("/api/upload")
async def api_upload(cwd: str = Form(...), file: UploadFile = File(...),
                     relpath: str = Form("")) -> dict[str, Any]:
    """Save one file into `cwd`.

    `relpath` is set when a whole folder was dragged in: the file's path
    inside that folder ("photos/2024/a.jpg"). Its folders are created under
    `cwd`; it can never climb out of it. A file that already exists is kept
    and the new one is saved next to it as "name (1).ext".
    """
    destination_dir = _resolve(cwd)
    if not destination_dir.is_dir():
        raise HTTPException(400, "Upload target is not a directory.")
    name = os.path.basename(file.filename or "upload.bin")
    if relpath:
        parts = [part for part in relpath.replace("\\", "/").split("/") if part]
        if not parts or any(part in (".", "..") or "\0" in part for part in parts):
            raise HTTPException(400, "Invalid folder path in the upload.")
        name = parts[-1]
        for part in parts[:-1]:
            destination_dir = destination_dir / part
            if destination_dir.exists() and not destination_dir.is_dir():
                raise HTTPException(409, f"{part} exists and is not a folder.")
            destination_dir.mkdir(exist_ok=True)
            # A symlinked folder inside the drop must not lead outside cwd.
            if not destination_dir.resolve().is_relative_to(_resolve(cwd)):
                raise HTTPException(400, "Invalid folder path in the upload.")
    destination = destination_dir / name
    counter = 1
    while destination.exists():
        destination = destination_dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
        counter += 1
    with destination.open("wb") as handle:
        while chunk := await file.read(1 << 20):
            handle.write(chunk)
    return {"ok": True, "path": str(destination), "name": destination.name}


# ----------------------------------------------------------------- favorites

def load_favorites() -> list[dict[str, str]]:
    if not FAVORITES_FILE.exists():
        return []
    try:
        data = json.loads(FAVORITES_FILE.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def write_favorites(items: list[dict[str, str]]) -> None:
    FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_FILE.write_text(json.dumps(items, indent=2))


@app.get("/api/favorites")
async def get_favorites() -> dict[str, Any]:
    """Listing prunes favorites whose directory has disappeared."""
    items = load_favorites()
    live = [item for item in items if Path(item["path"]).is_dir()]
    removed = [item for item in items if item not in live]
    if removed:
        write_favorites(live)
    return {"favorites": live, "removed": [item["path"] for item in removed]}


@app.post("/api/favorites")
async def add_favorite(payload: dict[str, Any]) -> dict[str, Any]:
    path = payload.get("path")
    if not path or not Path(path).is_dir():
        raise HTTPException(400, "That path is not a directory.")
    items = load_favorites()
    if any(item["path"] == path for item in items):
        return {"favorites": items, "added": False}
    items.append({"path": path, "label": payload.get("label") or Path(path).name or path})
    write_favorites(items)
    return {"favorites": items, "added": True}


@app.delete("/api/favorites")
async def remove_favorite(path: str) -> dict[str, Any]:
    items = [item for item in load_favorites() if item["path"] != path]
    write_favorites(items)
    return {"favorites": items}


# ---------------------------------------------------------------- sessions

@app.get("/api/sessions")
async def sessions_list() -> dict[str, Any]:
    """Every shell still alive on the server, including ones no tab shows.

    A tab that lost its session id (the browser was killed, the tab closed by
    accident) leaves its shell running here; this is how it is found again.
    Shells kept in tmux are listed even after a Shellwalk restart.
    """
    shells = []
    known = set()
    for item in registry.all():
        if not item.alive:
            continue
        known.add(item.id)
        shells.append({
            "id": item.id,
            "created": item.created,
            "cwd": item.cwd(),
            "running": None if item.is_idle() else item.foreground_command(),
            "attached": item.listener_count() > 0,
            "tmux": item.tmux,
        })
    for item in tmux_sessions():
        if item["id"] in known:
            continue
        running = item["running"]
        shells.append({**item, "running": None if running in (None, "bash", "zsh", "sh") else running,
                       "attached": False, "tmux": True})
    shells.sort(key=lambda entry: entry["created"])
    return {"sessions": shells, "tmux_available": tmux_available()}


@app.get("/api/workspace")
async def workspace() -> dict[str, Any]:
    """The open tabs, for a browser that is starting up.

    Closing the browser leaves every shell running; the next browser to open
    Shellwalk, on any device, gets the same tabs in the same order. None
    left means the browser opens one new tab.
    """
    settings = load_settings()
    shells = registry.workspace(shell=settings["shell"], cwd=settings["start_dir"],
                                scrollback_bytes=settings.get("scrollback_replay_bytes", 200_000))
    return {"tabs": [{"session": item.id, "title": item.title} for item in shells]}


@app.post("/api/session/title")
async def session_title(payload: dict[str, Any]) -> dict[str, Any]:
    """Name a tab, for every browser that shows it."""
    pty_session = registry.get(str(payload.get("session", "")))
    if pty_session is None:
        raise HTTPException(404, "That shell is no longer running.")
    title = str(payload.get("title", "")).strip()[:40]
    pty_session.title = title or None
    return {"ok": True}


@app.post("/api/sessions/adopt")
async def sessions_adopt(payload: dict[str, Any]) -> dict[str, Any]:
    """Make a tmux shell that outlived Shellwalk openable in a tab again."""
    session_id = str(payload.get("session", ""))
    if registry.get(session_id) is not None:
        return {"ok": True, "session": session_id}
    settings = load_settings()
    adopted = registry.adopt(session_id, shell=settings["shell"], cwd=settings["start_dir"],
                             scrollback_bytes=settings.get("scrollback_replay_bytes", 200_000))
    if adopted is None:
        raise HTTPException(404, "That shell is no longer running.")
    return {"ok": True, "session": adopted.id}


# ------------------------------------------------------------- assistant

@app.post(assist.ENDPOINT)
async def assist_endpoint(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Called from inside a shell by server/shell/assist_client.py.

    gate.py has already checked loopback and the per-shell token.
    """
    shell_id = request.headers.get("x-shellwalk-shell", "")
    return await assist.handle(payload, shell_id)


@app.post("/api/session/close")
async def session_close(payload: dict[str, Any]) -> dict[str, Any]:
    """End a shell outright — what closing a terminal *tab* means.

    The websocket surviving a dropped connection is the whole point of
    server/pty_session.py (a phone on mobile data should come back to the
    same shell). Closing a tab is a different act than a dropped connection:
    the person is done with that shell, not walking between cells, so this is
    the one place a session is actually torn down rather than kept warm.
    """
    session_id = payload.get("session")
    if session_id:
        registry.drop(session_id)
    return {"ok": True}


# ------------------------------------------------------------------ injection

@app.post("/api/inject")
async def api_inject(payload: dict[str, Any]) -> dict[str, Any]:
    """Type text into a session's shell. The single funnel for every click."""
    pty_session = registry.get(payload.get("session"))
    if pty_session is None:
        raise HTTPException(404, "No live session. Reload the page.")
    settings = load_settings()
    ok, reason = pty_session.inject(
        payload.get("text", ""),
        execute=bool(payload.get("execute", False)),
        clear_line=bool(payload.get("clear_line",
                                    settings.get("clear_line_before_inject", True))),
        require_idle=bool(payload.get("require_idle",
                                      settings.get("block_inject_when_busy", True))),
    )
    return {"ok": ok, "reason": reason}


# ------------------------------------------------------------ component invoke

@app.post("/api/invoke")
async def api_invoke(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a discovered component.

    Returns a small directive object the frontend applies:
      {"inject": str, "execute": bool}      type into the shell
      {"toast": str, "level": str}          notify
      {"open": url}                         open a new tab
      {"modal": {...}}                      ask the UI for something (e.g. a path)
      {"needs": ["target_path"]}            placeholders still unresolved
    """
    surface = payload.get("surface", "")
    component_id = payload.get("id", "")
    ctx = payload.get("ctx", {}) or {}
    ctx.setdefault("home", str(Path.home()))

    component = components.find(surface, component_id)
    if component is None:
        raise HTTPException(404, f"No component {surface}/{component_id}.")

    if component.get("action") == "route" and component.get("has_routes"):
        invoke = components.load_route(component)
        if invoke is None:
            raise HTTPException(500, "routes.py has no invoke() function.")
        result = invoke(ctx)
        if asyncio.iscoroutine(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True}

    template = component.get("command")
    if not template:
        return {"builtin": component_id, "component": component}

    command, missing = render(template, ctx, quote=component.get("quote", True))
    if missing:
        return {"needs": missing, "component": component, "partial": command}
    return {
        "inject": command,
        "execute": bool(component.get("execute", False)),
        "confirm": bool(component.get("confirm", False)),
        "confirm_message": component.get("confirm_message", ""),
    }


# ------------------------------------------------------------ context menus

def _menus() -> dict[str, Any]:
    components.invalidate()
    tree = components.discover(force=True)
    return {
        "ok": True,
        "surfaces": list(menu_editor.SURFACES),
        "context_menus": tree["context_menus"],
        "overrides": menu_editor.load_overrides(),
    }


@app.get("/api/context_menu")
async def context_menu_read() -> dict[str, Any]:
    return _menus()


@app.post("/api/context_menu/item")
async def context_menu_patch(payload: dict[str, Any]) -> dict[str, Any]:
    """Change one field of one action — usually the run-on-click switch."""
    try:
        menu_editor.patch(payload.get("surface", ""), payload.get("id", ""),
                          payload.get("values", {}) or {})
    except menu_editor.MenuError as exc:
        raise HTTPException(400, str(exc))
    return _menus()


@app.post("/api/context_menu/reset")
async def context_menu_reset(payload: dict[str, Any]) -> dict[str, Any]:
    menu_editor.reset(payload.get("surface", ""), payload.get("id", ""))
    return _menus()


@app.post("/api/context_menu/create")
async def context_menu_create(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        created = menu_editor.create(payload)
    except menu_editor.MenuError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(400, f"Could not create the action: {exc}")
    return {**_menus(), "id": created}


@app.post("/api/context_menu/delete")
async def context_menu_delete(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        menu_editor.remove(payload.get("surface", ""), payload.get("id", ""))
    except menu_editor.MenuError as exc:
        raise HTTPException(400, str(exc))
    return _menus()


# ---------------------------------------------------------- saved commands

def _commands() -> dict[str, Any]:
    components.invalidate()
    return {"ok": True, "commands": components.discover(force=True)["commands"]}


@app.get("/api/commands")
async def commands_read() -> dict[str, Any]:
    return _commands()


@app.post("/api/commands/create")
async def commands_create(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        created = command_editor.create(payload)
    except command_editor.CommandError as exc:
        raise HTTPException(400, str(exc))
    return {**_commands(), "id": created}


@app.post("/api/commands/item")
async def commands_patch(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        command_editor.patch(payload.get("id", ""), payload.get("values", {}) or {})
    except command_editor.CommandError as exc:
        raise HTTPException(400, str(exc))
    return _commands()


@app.post("/api/commands/delete")
async def commands_delete(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        command_editor.remove(payload.get("id", ""))
    except command_editor.CommandError as exc:
        raise HTTPException(400, str(exc))
    return _commands()


# --------------------------------------------------------------------- scripts

def _scripts() -> dict[str, Any]:
    components.invalidate()
    return {"ok": True, "scripts": components.discover(force=True)["scripts"]}


@app.get("/api/scripts")
async def scripts_read() -> dict[str, Any]:
    return _scripts()


@app.post("/api/scripts/create")
async def scripts_create(
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form(""),
    order: int = Form(200),
    style: str = Form("default"),
    confirm: bool = Form(False),
    confirm_message: str = Form(""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        created = script_editor.create(
            {"name": name, "description": description, "icon": icon, "order": order,
             "style": style, "confirm": confirm, "confirm_message": confirm_message},
            file.filename or "", await file.read())
    except script_editor.ScriptError as exc:
        raise HTTPException(400, str(exc))
    return {**_scripts(), "id": created}


@app.post("/api/scripts/item")
async def scripts_patch(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        script_editor.patch(payload.get("id", ""), payload.get("values", {}) or {})
    except script_editor.ScriptError as exc:
        raise HTTPException(400, str(exc))
    return _scripts()


@app.post("/api/scripts/code")
async def scripts_replace_code(
    id: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    try:
        script_editor.replace_code(id, file.filename or "", await file.read())
    except script_editor.ScriptError as exc:
        raise HTTPException(400, str(exc))
    return _scripts()


@app.post("/api/scripts/delete")
async def scripts_delete(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        script_editor.remove(payload.get("id", ""))
    except script_editor.ScriptError as exc:
        raise HTTPException(400, str(exc))
    return _scripts()


# ----------------------------------------------------------------------- LLM

@app.post("/api/llm/ask")
async def api_llm(payload: dict[str, Any]) -> dict[str, Any]:
    service = services.get("llm")
    if service is None or not hasattr(service, "ask"):
        return {"ok": False,
                "error": "No LLM service is active. Pick one in Settings › LLM."}
    result = service.ask(payload.get("prompt", ""), payload.get("context", ""))
    if asyncio.iscoroutine(result):
        result = await result
    return {"ok": True, "answer": result}


@app.get("/api/services")
async def api_services() -> dict[str, Any]:
    return services.describe()


@app.post("/api/services/active")
async def api_set_service(payload: dict[str, Any]) -> dict[str, Any]:
    category = payload.get("category")
    if category not in services.CATEGORIES:
        raise HTTPException(400, "Unknown service category.")
    services.set_active(category, payload.get("id", ""))
    return services.describe()


@app.post("/api/services/config")
async def api_service_config(payload: dict[str, Any]) -> dict[str, Any]:
    category = payload.get("category")
    if category not in services.CATEGORIES:
        raise HTTPException(400, "Unknown service category.")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(400, "Nothing to save.")
    try:
        services.set_user_config(category, str(payload.get("id", "")), values)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return services.describe()


@app.post("/api/services/models")
async def api_service_models(payload: dict[str, Any]) -> dict[str, Any]:
    """Models a service can reach, for the picker in Settings › Model.

    Takes the address being typed (not only the saved one) so the list can be
    checked before saving.
    """
    category = payload.get("category")
    service_id = str(payload.get("id", ""))
    if category not in services.CATEGORIES:
        raise HTTPException(400, "Unknown service category.")
    module = services.load_module(category, service_id)
    if module is None or not hasattr(module, "list_models"):
        return {"ok": True, "supported": False, "models": []}
    config = services.effective_config(category, service_id)
    if payload.get("base_url"):
        config["base_url"] = str(payload["base_url"]).strip()
    try:
        result = module.list_models(config)
        if asyncio.iscoroutine(result):
            result = await result
        return {"ok": True, "supported": True, "models": list(result)}
    except Exception as exc:  # unreachable host, bad URL, ...
        return {"ok": False, "supported": True, "models": [], "error": str(exc)}


# ------------------------------------------------------------------ settings

@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return load_settings()


@app.post("/api/settings")
async def patch_settings(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return save_settings(payload)
    except SettingsError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------- project update

def restart_in_place() -> None:
    """Replace this process with a fresh run.py: same PID, nothing left in memory.

    run.py applies an armed update at the top of main(), before server/ is
    imported, and re-reads settings.json (a new bind address) on the way up.
    There is no supervisor to hand this to: the process restarts itself.

    Every descriptor above stdio is closed first. The listening socket and
    the PTY masters (pty.fork leaves them inheritable) would otherwise cross
    the exec: the port would stay taken by nobody, and shells that should get
    SIGHUP would live on, unreachable. Tmux shells are not our children and
    carry on, as they do across any restart.
    """
    try:
        argv = json.loads(os.environ.get("SHELLWALK_ARGV", "[]"))
        if not (isinstance(argv, list) and all(isinstance(a, str) for a in argv)):
            argv = []
    except json.JSONDecodeError:
        argv = []
    os.closerange(3, max(1 << 16, os.sysconf("SC_OPEN_MAX")))
    # Reap the children that die of that SIGHUP, so they do not linger as
    # zombies of the new process. A child that ignores it is left behind.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            time.sleep(0.02)
    import sys
    os.execv(sys.executable, [sys.executable, str(ROOT / "run.py"), *argv])


def _schedule_restart() -> None:
    """Restart shortly, giving the response time to reach the browser first.

    The same path serves an applied update and a bind-host change: an update
    has a plan for run.py to apply on the way up, a bind-host change has
    nothing to apply, it only needs settings.json read fresh at startup.
    """
    async def restart() -> None:
        await asyncio.sleep(0.6)
        restart_in_place()

    asyncio.create_task(restart())


def _sessions_at_risk() -> list[dict[str, Any]]:
    """Shells a restart would kill, and what they are running.

    Specific to this app: unlike a normal web service, restarting here drops
    live terminals. Losing a prompt is nothing; losing a half-finished rsync
    matters, so the preview says so before you commit.
    """
    at_risk = []
    for session in registry.all():
        if not session.alive or session.tmux:
            continue                    # tmux shells survive a restart
        at_risk.append({
            "id": session.id,
            "idle": session.is_idle(),
            "running": session.foreground_command(),
            "cwd": session.cwd(),
        })
    return at_risk


@app.get("/api/update/status")
async def update_status() -> dict[str, Any]:
    status = update_plan.pending_status(ROOT)
    status["sessions"] = _sessions_at_risk()
    status["roots"] = list(update_plan.MERGE_ROOTS)
    status["files"] = list(update_plan.MERGE_FILES)
    status["data_home"] = str(DATA_HOME)
    return status


@app.post("/api/update/preview")
async def update_preview(file: UploadFile = File(...)) -> dict[str, Any]:
    """Stage an uploaded zip and return the plan. Nothing is applied yet."""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "The uploaded file is empty.")
    try:
        staging = update_plan.stage_zip(ROOT, raw, file.filename or "update.zip")
        plan = update_plan.build_plan(ROOT, staging)
        update_plan.write_plan(ROOT, plan)
    except update_plan.UpdateError as exc:
        update_plan.clear_pending(ROOT)
        raise HTTPException(400, str(exc))
    plan["sessions"] = _sessions_at_risk()
    return plan


@app.post("/api/update/apply")
async def update_apply(payload: dict[str, Any]) -> dict[str, Any]:
    """Arm the plan and restart.

    The merge is deliberately not performed here. This process re-executes
    run.py, which applies the plan before the app's code is loaded again.
    See server/update_plan.py for why.
    """
    try:
        plan = update_plan.arm(ROOT, payload.get("token", ""))
    except update_plan.UpdateError as exc:
        raise HTTPException(400, str(exc))

    _schedule_restart()
    return {"ok": True, "restarting": True, "counts": plan.get("counts", {})}


@app.post("/api/update/cancel")
async def update_cancel() -> dict[str, Any]:
    update_plan.clear_pending(ROOT)
    return {"ok": True, **update_plan.pending_status(ROOT)}


# What the backup carries: DATA_HOME, which is everything that makes this
# install yours — settings, saved commands, favorites, service choices, your
# own menu actions and scripts. The code is not in it any more: it is the same
# for everyone and comes from the repository.
#
# Deliberately NOT included, even though they live in DATA_HOME:
#   auth/    secret_key, auth.json, devices, the assist key — this machine's
#            own credentials; shipping them means whoever holds the zip can
#            mint sessions for this machine
#   run/     a live pid file, meaningless off this machine
BACKUP_SKIP_TOP = ("auth", "run")


@app.get("/api/update/backup")
async def update_backup() -> Response:
    """Download a personal backup: DATA_HOME minus credentials.

    Paths in the zip are relative to DATA_HOME, so unzipping it into an empty
    DATA_HOME (by default ~/.config/shellwalk) restores it. Streams from a
    temporary file so a large data folder does not have to fit in memory.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    handle, temp_path = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    skip_dirs = set(update_plan.SKIP_DIRS)

    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            for path in sorted(DATA_HOME.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(DATA_HOME)
                if relative.parts[0] in BACKUP_SKIP_TOP:
                    continue
                if any(part in skip_dirs for part in relative.parts):
                    continue
                if path.suffix.lower() in update_plan.SKIP_SUFFIXES:
                    continue
                archive.write(path, relative.as_posix())
    except OSError as exc:
        os.unlink(temp_path)
        raise HTTPException(500, f"Could not build the backup: {exc}")

    return FileResponse(
        temp_path,
        filename=f"shellwalk-settings-{stamp}.zip",
        media_type="application/zip",
        background=BackgroundTask(lambda: os.unlink(temp_path)),
    )


# --------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text())


class AppStatic(StaticFiles):
    """Static files that always revalidate.

    Every update replaces web/js/*.js while the URL stays the same, and a
    browser holding a cached module will happily run last week's frontend
    against this week's backend — which looks exactly like a broken feature
    ("api.menus is not a function") and sends you hunting in the wrong place.
    "no-cache" does not mean "do not store": the file is still cached, the
    browser just has to ask first, and ETag turns that into a 304 on a
    connection that is either loopback or your own tunnel.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


app.mount("/static", AppStatic(directory=str(WEB_DIR)), name="static")

# The gate wraps everything defined above — HTTP routes, the static mount, and
# every websocket. It must be the outermost layer, or a route registered
# after this line would bypass it entirely.
app = GateMiddleware(app)
