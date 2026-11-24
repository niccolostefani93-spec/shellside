# Shellside

A real Linux shell in the browser, with the navigation friction removed.

The terminal is a genuine PTY — vim, htop, `tail -f`, background jobs and ssh
all work, because nothing is being simulated. Layered on top is a clickable
breadcrumb, a live folder panel, saved commands and context menus, all of which
work by typing into that same shell.

    bash setup.sh
    .venv/bin/python run.py           # http://127.0.0.1:8022

Use `bash setup.sh`, not `./setup.sh` — a downloaded archive does not always
keep the executable bit, and `bash` runs the script either way. If you would
rather fix the bit once and use `./setup.sh` from then on: `chmod +x setup.sh`.

`setup.sh` builds the virtualenv, upgrades pip inside it *before* installing
anything, installs the dependencies and then verifies that each one actually
imports. Pass an interpreter to use a specific one: `./setup.sh python3.13`.

That ordering matters. An old pip inside a fresh virtualenv is the usual reason
a package lands half-written — present on disk, missing a subpackage — which
then fails at request time rather than at install time. A half-installed `anyio`
is the classic: the app appears to boot, then serves 500s with no CSS, because
`anyio._backends._asyncio` is what Starlette uses for every static file.

If you would rather do it by hand:

    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python run.py

On Ubuntu 24.04 and other PEP 668 systems, installing outside a virtualenv is
refused by the OS; `pip3 install --break-system-packages -r requirements.txt`
is the escape hatch. If anything is missing — or installed but incomplete —
`run.py` says so and prints the exact command rather than trying to start.

**Very new Python versions:** some wheels are still built from source and can
fail half way. `run.py` will only ever suggest an interpreter that is actually
installed on your machine.

By default the app listens on loopback only, so `127.0.0.1:8022` works and the
machine's LAN address does not. That is deliberate — it hands out a root-capable
shell, so the intended route in is the Cloudflare tunnel, even from home. To
reach it from another device on your own network instead:

    .venv/bin/python run.py --host 0.0.0.0

and read the warning it prints. If it still will not connect from another
machine, the firewall is next: `sudo ufw allow 8022/tcp`.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [Where this differs from the handoff document](#where-this-differs-from-the-handoff-document)
- [Shells that outlive the browser, and Shellside](#shells-that-outlive-the-browser-and-shellside)
- [Drag and drop: upload and download](#drag-and-drop-upload-and-download)
- [On a phone](#on-a-phone)
- [The command assistant](#the-command-assistant)
- [Adding things without touching code](#adding-things-without-touching-code)
- [Services](#services)
- [Deploying behind Cloudflare](#deploying-behind-cloudflare)
- [Layout](#layout)

---

## How it fits together

Two websockets, deliberately kept apart:

| Socket | Carries | Why separate |
|---|---|---|
| `/ws/terminal` | Raw PTY bytes, both directions | Hot path. Nothing else belongs on it. |
| `/ws/control` | Folder listings, cwd changes, shell status | Keeps directory data out of the visible log |

The folder panel never runs `ls`. The backend watches the PTY's cwd and reads
the directory with `os.scandir`, so navigating costs one syscall and leaves no
trace in your scrollback.

Every clickable shortcut ends in the same place: text written into the PTY's
stdin, as if you had typed it. The UI never interprets commands.

**Navigation runs. Everything else pastes.** Clicking a breadcrumb segment or a
folder executes the `cd` immediately, because the panel has to follow the shell.
Saved commands, context-menu actions and model suggestions are pasted and left
at the prompt for you to press Enter — including destructive ones.

---

## Where this differs from the handoff document

Six deviations. The first two are corrections; the rest are hardening.

### 1. cwd comes from `/proc`, never from parsing stdin

§2.1 offered a choice: detect the PTY's working directory, or be "informed of
`cd` commands as they are sent". The second option cannot be made to work.

This implementation reads `/proc/<foreground-pgid>/cwd` — the kernel's own
answer — via `os.tcgetpgrp()` on the PTY master. Tested side by side:

| Input | Parsing stdin | Reading `/proc` |
|---|---|---|
| `cd /tmp` | correct | correct |
| `cd -` | wrong | `/tmp` |
| `pushd /var/log` | wrong | `/var/log` |
| `T=/etc; cd $T` | wrong | `/etc` |
| `goto(){ cd /usr/share; }; goto` | wrong | `/usr/share` |
| `cd 'dir with spaces'` | fragile | correct |

The same call also reports whether the shell is at its prompt and what is
running, which drives the status light at the right of the breadcrumb.

**Linux only.** `/proc` is the mechanism. On macOS or BSD the cwd would need
`libproc` or `lsof`; `PtySession.cwd()` is the single place to change.

### 2. Injection refuses to type into a running program

"Write into the PTY's stdin as if typed" is right, but if vim owns the terminal
then injecting `rm -rf {file_path}` types that *into vim*. Before injecting,
the backend checks that the shell itself holds the foreground process group:

    during `sleep 3` → inject blocked: "Terminal is busy running sleep. Exit it first."

Both this and the Ctrl-U line-clear that precedes a paste are switchable in
**Settings › General**.

### 3. Substituted values are shell-quoted

The document's own example, `unzip {file_name} -d {cwd}`, breaks on
`my archive.zip`. Worse, for a file named `a b; rm -rf ~`:

    unquoted →  rm -rf /tmp/a b; rm -rf ~      ← deletes your home directory
    quoted   →  rm -rf '/tmp/a b; rm -rf ~'    ← what you meant

Quoting is on by default; a manifest can opt out with `"quote": false` when it
genuinely wants globbing or word splitting.

### 4. The `|command|` convention needed a disambiguation rule

Pipes are shell syntax, so `|ps aux | grep ssh|` is ambiguous. The rule:

- a line that **starts and ends** with a pipe is one command, inner pipes included
- otherwise pipe pairs match non-greedily, for inline mentions

So `|ps aux | grep -i ssh|` becomes a single correct chip, `Run |ls -la| now.`
chips just the command, and `a | b` in prose chips nothing. The system prompt in
each LLM service tells the model to put commands alone on their line, which
keeps it in the unambiguous case.

### 5. Loopback by default, and an honest note about the LAN

§7 treats Cloudflare Zero Trust as the access boundary. That is true of the
tunnel and false of the LAN, where the app listens with nothing in front of it —
so "reachable from the local network" means an unauthenticated shell on your
network.

`run.py` binds `127.0.0.1` by default; reach the app through the tunnel even at
home. Binding wider prints a warning unless a non-pass-through security service
is active. A working `shared_token` module ships alongside the pass-through.

The deferred device-challenge and lockout design is **not** built, as specified.

### 6. Vendored xterm, and sessions that survive a dropped connection

xterm.js is served from `web/vendor/` rather than a CDN, so the app opens on a
network that blocks everything except your tunnel.

PTY sessions outlive their websocket. A phone that drops its connection
reconnects to the same shell and gets its scrollback replayed — which for goal
two is the difference between usable and not. See *Shells that outlive the
browser, and Shellside* below for what survives what.

Two smaller judgement calls, both reversible in one line: dotfiles are filtered
before the directory check, so `.git` and `.venv` don't clutter the panel; and
a file is visible exactly when a handler lists its extension (see *File
handlers* below), so there is no separate visibility list to keep in step.

---

## Shells that outlive the browser, and Shellside

| what happens                                   | plain shell | shell in tmux |
| ---------------------------------------------- | ----------- | ------------- |
| phone locked, network drops, page reloaded     | survives, tab reattaches | survives, tab reattaches |
| browser closed or killed                       | survives, every tab comes back on the next browser, any device | same |
| a Shellside tab closed (✕)                     | **ends** — on every device | **ends** — on every device |
| Shellside restarted or updated                 | **ends**    | survives, tab reattaches |
| the machine reboots                            | ends        | ends          |

A long job — a build, an rsync, a Claude Code session — keeps running while
the phone sleeps either way.

**The open tabs live on the server** (`/api/workspace`), not in the browser.
Close the browser with three tabs and the next browser to open Shellside —
the phone, another computer — opens with those three, names included; close
it with none left running and it opens with one new shell. Closing a tab is
different: it ends that shell, and any other browser showing it drops the tab
too. Only which tab was on screen is remembered per browser.

**↺ Running shells** (next to `+` in the tab bar) lists every shell still alive
on the server, including ones no tab shows, with its folder and what it is
running. *Open* puts it back in a tab; *End* closes it.

**Settings › General › Keep shells in tmux** starts each new shell inside
Shellside's own tmux server (`tmux -L shellside`, config in
`server/shell/tmux.conf`). The browser tab keeps the shell's id, so after a
Shellside restart it reconnects to the same shell without you doing anything.
The config hides tmux completely: no status bar, no prefix key, and no
alternate screen, so scrolling on a phone works as before. Your own tmux
sessions and `~/.tmux.conf` are not touched. Needs `sudo apt install tmux`.

With tmux the shell is not Shellside's child, so the idle check and the
folder panel read the pane's shell from `/proc/<pid>/stat` (its terminal's
foreground process group) instead of `tcgetpgrp()` on Shellside's own PTY.

---

## Drag and drop: upload and download

Drag files or whole folders from your computer onto the terminal or the
folder panel: they are uploaded into the shell's current folder. Drop them on
a folder in the panel to upload into that folder instead; it lights up while
you hover it. A dropped folder keeps its structure. A file that already
exists is never overwritten: the new one is saved next to it as
`name (1).ext`. A progress box shows the file, the count and the bytes sent.

The right-click **Upload** item still works, and is the way on a phone.

**The other way round.** Drag a file or folder from the folder panel out of
the browser, onto the desktop or into a folder, to download it; a folder
arrives as `name.zip`. Drop it on the terminal instead and its path is typed
at the prompt, quoted and not run, as desktop terminals do.

Dragging out of the browser relies on `DownloadURL`, which Chromium browsers
(Chrome, Edge, Brave) understand and Firefox and Safari do not; there, use
the right-click **Download** item. Folder zips are built while they download
(`/api/download` on a folder), so a folder of any size starts at once and
never touches the disk. Files that are already compressed (images, video,
archives, model weights) are stored rather than compressed again; symlinks
that point outside the folder are left out.

Uploads go to `/api/upload`. A folder drop sends each file's path inside the
dropped folder as `relpath`; its folders are created under the target, and
`..`, empty parts and symlinks leading outside the target are refused.

---

## On a phone

On a narrow screen only the tab bar and the folder path sit above the
terminal. **☰** at the end of the tab bar opens the commands bar and the folder
panel; they close again by themselves when the keyboard comes up. With the
keyboard open the folder path hides too, and the page shrinks to the space
above the keyboard rather than being covered by it.

**⛶** next to it switches the browser to full screen, hiding its address bar
and the system bars (on browsers that allow it; iPhone Safari does not).

Drag the terminal with a finger to scroll back through the output; a flick
keeps going. The drag is turned into mouse-wheel events on the terminal, so in
vim, less or a full-screen program it does what the wheel does there. A tap
still opens the keyboard.

Touch screens get a key bar at the bottom with what a phone keyboard lacks:
Esc, Tab, Shift+Tab, sticky **Ctrl** and **Alt** (tap, then a key; tap both
for Ctrl+Alt; they also modify the arrows, so Ctrl then → jumps a word),
Ctrl+C, the arrows (hold to repeat), `| ~ / -`, Ctrl+D and Ctrl+R. The bar
scrolls sideways. **A− / A+** at its end set the text size for that device only;
the size in Settings › General applies everywhere and resets that choice.

---

## The command assistant

When bash cannot find a command, the line goes to the active model and its
suggestions are printed in the terminal, as ordinary output:

    $ makedire cane
    bash: makedire: command not found
      Looks like a typo for mkdir.
      ▸ mkdir cane
      tap a command to paste it, then press Enter

The same works for a request written in plain words (`create a folder called
cane`), since its first word is not a command either. Tapping a suggestion
pastes it; nothing runs until you press Enter. To ask on purpose — for a
request that starts with a real command word — begin the line with a comma:

    $ , find the five biggest files here

**Settings › Model** turns it on and off, and picks the model. It stays quiet
while the *Echo* service is active, telling each shell once that no model is
set up.

How it works:

- Shellside starts bash as `bash --rcfile server/shell/bashrc -i`. That file
  loads `/etc/bash.bashrc` and `~/.bashrc` first, exactly as plain `bash -i`
  would, then defines bash's `command_not_found_handle` and the `,` command.
  Other shells start as before, without the assistant.
- The handler runs only for a person at a terminal: never in scripts, pipes
  or `$(...)`. Several failures within two seconds (a loop) call the model
  once. A distribution's own handler (Ubuntu's package hints) still runs
  whenever the assistant stays silent.
- `server/shell/assist_client.py` (standard library only) posts the line to
  `/internal/assist`. That endpoint skips the cookie check but accepts only
  loopback connections carrying a token derived for that shell from a key in
  `.auth/assist_key`. Loopback alone would not do: behind a tunnel,
  internet traffic arrives from 127.0.0.1 too.
- Suggestions are printed as OSC 8 hyperlinks with a `shellside-paste:` URI;
  the browser terminal turns a click on one into a paste. Being real output,
  they are in the scrollback after a reconnect.

Limit: bash parses the line before the assistant sees it, so a request with an
unbalanced quote (`l'ultima cartella`) leaves bash waiting for the closing
quote. Press Ctrl-C, and wrap such a request in double quotes.

---

## Adding things without touching code

Every pluggable unit is a folder with a `manifest.json`, optionally a
`command.txt` or a `routes.py`. The top bar, settings tabs and all context menus
are built by walking those folders at startup and whenever their mtimes change.
There is no registry to keep in sync.

**Saved commands** (the chips on the left of the top bar) are the exception:
they need no folder. Click the **+** after the last chip, or right-click the
empty part of the top bar, to make one; manage them all in Settings › Commands.
Each one is written to `app/commands/<id>.json`.

A new context-menu action, in full:

    app/context_menu/file_entry/checksum/
      manifest.json    {"name": "Checksum", "icon": "#", "order": 55,
                        "match": {"extensions": [".iso", ".img"]}}
      command.txt      sha256sum {file_path}

Reload from **Settings › Reload config from disk**, or just navigate — the mtime
scan picks it up.

**Placeholders:** `{file_name}` `{file_path}` `{cwd}` `{target_path}`
`{selection}` `{home}`

**Manifest keys:** `name` `description` `icon` `order` `enabled` `style`
(`default|danger|warning|info`) `bold` `confirm` `confirm_message` `quote`
`execute` `match.extensions` `match.kinds` `needs_picker` `picker_title`

`bold: true` marks a default action; the menu renders those first and rules a
line under them, per §3.5.

When a single templated command isn't enough, add `routes.py` with an
`invoke(ctx)` returning any of `inject` `execute` `toast` `level` `open`
`modal`. `app/scripts/shutdown/routes.py` is a worked example.

The Move action shows the `needs_picker` mechanism: it collects
`{target_path}` from a folder-picker modal that reuses the **same** `Navigator`
widget as the main panel, so the two cannot drift apart.

---

## Services

Cross-cutting integrations live under `services/<category>/<name>/` and are
selected by `active.json`. Core code never imports a connector.

| Category | Interface | Ships with |
|---|---|---|
| `llm` | `ask(prompt, context) -> str` | `ollama`, `openai_compatible`, `echo` |
| `display` | `ensure()`, `launch(cmd, ctx)`, `status()`, `stop()` | `x11vnc`, `none` |

`echo` is an offline stub that returns a canned answer containing both a plain
and a piped command, so the whole Ask-the-model round trip can be exercised
without a model installed. Switch services in **Settings › Model / Access**.

Adding a backend is adding a folder and repointing `active.json`. No core file
changes, ever.

---

## Deploying behind Cloudflare

    python3 run.py                                    # loopback
    cloudflared tunnel --url http://127.0.0.1:8022

Put the hostname behind a Zero Trust Access policy. Keep the bind on loopback so
the tunnel is the only route in, even from your own network.

If you do want a LAN route, switch **Settings › Access** to `shared_token` and
start with `SHELLSIDE_TOKEN` exported.

Preview of `.html` and `.svg` is served with `Content-Security-Policy: sandbox`.
Without it, a previewed file executes script on the origin that owns your shell.

---

## Access control

**Settings › Access.** One LAN password, real sessions, and a way back in if
you ever lock yourself out. This replaced an earlier version where security was
a swappable service — picked by `services/security/active.json`, checked by a
function that fell back to *allow* whenever the service failed to load. That
design has a fatal flaw: a missing folder, a typo, an import error, and every
request sailed through with nothing saying so. It also happened to be
unusable in practice — `shared_token` checked an HTTP header, and **browsers
cannot set custom headers on a WebSocket handshake**. The terminal lives on a
websocket, so that mechanism could never have worked from an actual browser,
regardless of configuration.

Both are fixed the same way: the gate is now unconditional core
(`server/gate.py`, `server/auth.py`), wired into every request — HTTP and
websocket alike — before any route runs. There is no `active.json` for it and
no way to disable it by accident.

### How trust works

    started by a foreign web page            →  refused, before anything else
    this machine's own browser, bound to
      127.0.0.1 only, tunnel mode off         →  trusted outright, no cookie needed
    a valid session or device cookie         →  trusted
    anything else                            →  denied

**Cross-site requests.** Every websocket and every POST, PUT, PATCH or
DELETE must come from one of Shellside's own pages: its `Origin` header has to
match the address the request was sent to (`Host`, or `X-Forwarded-Host` when
a proxy rewrites `Host`). Browsers set `Origin` themselves and a page cannot
forge it, so another website you happen to visit cannot open
`/ws/terminal` with your cookies, or with loopback trust, and get a shell.
This is Cross-Site WebSocket Hijacking, and `SameSite=Lax` cookies do not
stop it from a sibling subdomain. A websocket with no `Origin` at all is
refused too; plain requests without one (curl, scripts) are left to the
other checks. If your proxy passes neither the public `Host` nor
`X-Forwarded-Host`, list the public address in `app/settings.json`:

    "allowed_origins": ["https://shell.example.com"]

**Loopback trust is narrow on purpose.** It applies only when all of these
hold: the TCP peer is 127.0.0.1 or ::1; the app is bound to 127.0.0.1 only;
the request carries no proxy headers (`Cf-Connecting-Ip`, `X-Forwarded-*`,
`Forwarded`…); and its `Host` is `localhost`, `127.0.0.1` or `[::1]`. The
proxy-header rule means traffic relayed by `cloudflared` or a reverse proxy on
the same machine never gets free trust, even with tunnel mode off. The `Host`
rule defeats DNS rebinding, where a website points its own name at 127.0.0.1
to reach local services: the browser still sends that website's name as
`Host`. Binding `0.0.0.0` switches loopback trust off entirely.

Still, if Shellside is reachable through a tunnel, turn on **tunnel mode** or
set an **internet password** (which turns it on): then loopback trust is off
altogether and every browser needs a session, as a second layer behind
whatever protects the tunnel (Cloudflare Access, for example).

Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` whenever the browser
reached Shellside over HTTPS.

A denied HTTP page load redirects to `/auth/login`; a denied API call gets a
plain 401; a refused cross-site request gets 403. A denied websocket is
closed at the handshake (4401 not signed in, 4403 cross-site), before the
PTY, the folder listing, or anything else it touches ever runs.

### The LAN password

Set from **Settings › Access**. Hashed with
`hashlib.scrypt` — stdlib, no new dependency — so a forgotten password is a
reset, not something anyone with read access to `.auth/` could read back
directly.

Binding `0.0.0.0` with no password set **denies everyone but the local
machine**, rather than quietly staying open — `run.py` says so plainly in its
startup banner. "Allow the LAN with no password" is a distinct, explicit
toggle for households that want that; it's not something an empty text field
falls into by accident.

Changing the password, or pressing **Sign out every device**, bumps a session
epoch that invalidates every other signed-in browser immediately. The one
exception: a session from the recovery link below is exempt from that epoch
on purpose, since it's the thing you'd use to fix a lockout in the first
place.

### If you lock yourself out

A fresh recovery link is generated every boot and printed to the terminal
that started the app:

    Shellside is up. Signed out? This link signs you back in from this
    machine, no password:
        http://127.0.0.1:8022/auth/local?token=...

It works from loopback only, is not consumed on use — losing that browser tab
doesn't burn it — and dies the moment the process restarts. It's also shown
in **Settings › Access** while you're signed in, in case you want to bookmark
it for next time. Whatever else breaks — a forgotten password, a wrong
setting, a bad update — sitting at the actual machine always gets you back
in.

### Attempts and lockouts

Configurable in **Settings › Access**: seconds between attempts (default 3),
failures before one address is blocked (default 5, 15 minutes), and failures
overall before sign-in freezes completely (default 20, cleared manually from
the same panel). All in-memory, reset on restart — this is a home appliance,
not a service worth persisting attacker bookkeeping across reboots. Verified
directly: five wrong passwords from one address blocked it, and the sixth
attempt was rejected even with the correct password.

### `.auth/`

Everything lives under `.auth/` at the project root — never inside `app/`,
`server/`, or `services/`, which an update or a shared config export might
touch. Directory mode `0700`, files `0600`. Not swept by the update system:
`.auth/` is outside every merge root on purpose.

---

## Opening files, and running graphical programs

### File handlers

**Settings › File types** holds one ordered list of *handlers*. A handler is a
name, a group of extensions, and one of four kinds:

| kind      | what a click does                                          |
| --------- | ---------------------------------------------------------- |
| `browser` | opens the file in a new tab, served from `/api/file`       |
| `editor`  | opens it in the built-in text editor                       |
| `command` | types `command` into the terminal                          |
| `desktop` | types `command` with `DISPLAY` set, backgrounded, and opens the window view |

`command` and `desktop` handlers also say whether they **run** at once or
**paste** and wait for Enter. Placeholders: `{file_path}` `{file_name}`
`{file_dir}` `{cwd}`, each shell-quoted.

That one list decides three things:

- **Left click** — the first handler, top to bottom, that lists the extension
  and is set to *Left click*. Move a handler up to make it win.
- **Right click** — every handler that lists the extension, the left-click one
  first and in bold, then the generic actions (download, rename, move…). So the
  left click is always also a menu entry; a folder's menu starts with *Open*.
- **Visibility** — a file shows in the folder panel when any handler lists its
  extension. Folders and executables always show; dotfiles follow their switch.

Stored as `handlers` in `app/settings.json`. A settings file from before
handlers existed is converted the first time it is read: customised click
actions become their own handlers at the top, and any extension that was only
*visible* is added to *Open with system default*.

### Files the browser can render

Photos, music, video, PDFs, text and JSON open in a new tab, served from
`/api/file`. **HTTP Range requests work**, so you can scrub through a video and
Safari will play it — media needs `206 Partial Content` or it refuses.

    Range: bytes=1000000-1000999
    → 206, content-range: bytes 1000000-1000999/3000000

The limit is codecs, not plumbing: an `.mp4` containing H.265 will not play in
Chrome no matter how well it is served. That is what the display below is for.

### Graphical programs on a headless host

There is no monitor, so the app runs a virtual one. `Window view` in the top bar
starts an Xvfb X server, points x11vnc at it, and opens a viewer tab. Any X11
program launched with `DISPLAY` set shows up there — mouse, keyboard, clipboard
and window dragging all work.

    sudo apt install xvfb x11vnc openbox

Nothing starts until you open the window view for the first time.

**The VNC port never leaves loopback.** The browser reaches it through
`/ws/window` on the app's own port, which pipes the websocket straight to the
VNC socket — the job websockify would normally do as a separate daemon. One
port, one origin, one tunnel hostname, and the Zero Trust policy you already
have covers the graphical session too.

Two ways in:

- **Window view** (top bar) — opens the desktop; launch anything from your own
  shell with `DISPLAY=:99 gimp &`
- **A `desktop` handler** (click a file) — the shipped ones use `mpv` for
  `.mkv`, `gimp` for `.psd`, `libreoffice` for `.docx` and `xdg-open` for a few
  others; add or change them in Settings › File types. The command is typed
  with `DISPLAY` already set and the viewer tab opens

By default it **pastes and waits** for you to press Enter. The
command runs in your own shell, so it inherits your environment, appears in your
history, and dies on Ctrl-C. A GUI program is not a special kind of process
here; it just has `DISPLAY` set. Typing that line yourself does exactly the
same thing.

Geometry, display number and window manager live in
`services/display/x11vnc/manifest.json`. Without a window manager, windows have
no title bars and cannot be moved — openbox is tiny and worth installing.

Switch to the `none` backend in **Settings › Display** and nothing graphical
runs on the host at all.

**Cost:** VNC ships pixels. A static window is nearly free; full-screen video
over VNC is not, and 4K playback will be smoother downloaded than streamed this
way. For heavy 3-D or video, xpra's HTML5 client or KasmVNC would encode better —
both drop in as another `services/display/` folder without touching core code.

---

## Updating the project, and backing it up

**Settings › Update / Backup.** Import a zip of new code, review exactly what it
will do, apply it, and download the whole project.

### Why run.py is a supervisor

The service that updates the code runs *inside* the process being updated. If it
overwrote its own files while alive, the process would be left with old code in
memory and new code on disk — and on a headless box that is not fixable
remotely.

So the merge never happens in the app. It happens in `run.py`, which runs the
application as a child and outlives it:

    UI ──▶ preview ──▶ confirm
                          ├─ content validated into _pending_update/payload/
                          ├─ plan armed as _pending_update/manifest.json
                          └─ restart requested (exit code 42)
                                       │
                                  run.py (app is DOWN)
                                       ├─ copies the files in the plan
                                       ├─ performs the deletions
                                       ├─ deletes _pending_update/
                                       └─ starts the app again

Until `manifest.json` exists, `_pending_update/` is inert rubbish: an abandoned
preview has no effect and is overwritten by the next one. If the machine loses
power between confirming and restarting, the update is applied on the next
start. `python3 run.py --no-supervisor` runs the old way, and the panel says so.

### How an update zip is shaped

> An update always starts from `server/`, `web/`, `app/` or `services/`. The
> path inside the zip **is** the destination path.

No searching by name, no ambiguity:

    update.zip
    ├── server/update_plan.py
    ├── app/context_menu/file_entry/checksum/manifest.json
    └── delete.json               ← optional, top level, beside the roots

Three single files are also accepted at that level: `README.md`, `run.py` and
`requirements.txt`. A zip named `server.zip` is taken as the *contents* of
`server/`. A lone wrapper folder is descended into automatically.

Ignored, and listed in the preview: anything outside those roots, `__pycache__`,
`.pyc`, and files already present that the update does not name. Updating 2 of a
folder's 5 files leaves the other 3 untouched.

`delete.json` removes things — the merge itself only adds and overwrites:

    {"delete": ["app/context_menu/file_entry/old_action",
                "server/legacy.py"]}

Each entry is a full relative path inside a merge root, pointing deeper than the
root itself. `"server"` alone is refused, paths outside the perimeter are
warned and skipped, and `delete.json` is never copied into the project. Copies
always run before deletions.

### The three defences on run.py

The supervisor can update itself — it is the most delicate file here, so:

1. incoming `.py` files are **compiled at preview time**; a broken one fails the
   analysis and can never be armed
2. `run.py` recompiles again before overwriting itself and keeps the current
   version if it does not compile
3. the previous version is kept as `run.py.bak` — not an automatic rollback, the
   file to restart from by hand

Having replaced itself, the supervisor re-executes immediately so the new code
takes effect in that same restart.

There is **no automatic backup and no automatic rollback.** If the app does not
come back, the supervisor keeps trying and prints its logs; diagnosis is yours.
Take a backup when you want one, with the button.

### What is specific to this app

A restart here closes every open shell. The preview checks each live PTY and
names anything mid-run — `rsync in /srv — the restart will kill it` — before you
commit. Losing a prompt is nothing; losing a half-finished job is not.

### The backup

Downloads the project as a zip, in the shape it is handed over in. Everything is
included except `__pycache__`, `.pyc`, `.git` and `_pending_update/`, so it can
be large and takes a few seconds to build. It streams from a temporary file, so
project size is not limited by memory.

---

## Layout

    server/          FastAPI backend
      pty_session.py   PTY, cwd detection, idle guard, session registry
      fsview.py        directory listing and file classification
      components.py    filesystem-as-configuration discovery
      templating.py    placeholder substitution with quoting
      services.py      pluggable service loader
      assist.py        command assistant: prompt, answer parsing, shell tokens
      shell/           what runs inside the shell: bashrc, assist_client.py,
                       tmux.conf
      main.py          routes and websockets

    app/             your configuration, all folder-driven
      scripts/         right side of the top bar
        settings_panel/tabs/    one folder per settings tab
      commands/        saved commands, one JSON each
      context_menu/    one folder per surface, one per action
      favorites/       paths.json
      data/            reference files for LLM context

    services/        llm/, security/ and display/, one folder per implementation
    web/             frontend; vendor/ holds xterm

---

## Known gaps

- `Move`'s picker lists directories only; moving into a path you cannot browse
  to needs typing.
- The mtime scan is O(files under `app/`). Fine at this scale; swap in
  `watchfiles` if that tree ever grows large.
- The graphical display has no password of its own; it inherits whatever guards
  the app. That is correct behind Zero Trust and wrong on an open LAN — the same
  caveat as everything else here.
- No audit log of executed commands. Worth adding before this faces the
  internet, as the document itself notes.
- Settings edits cover the common fields; the rest of `app/settings.json` is
  hand-edited.
