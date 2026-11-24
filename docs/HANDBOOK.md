# Shellside handbook

How Shellside works, how to extend it, and why it is built the way it is. To
install it, see the [README](../README.md): `bash setup.sh`, three questions.

A real Linux shell in the browser, with the navigation friction removed. The
terminal is a genuine PTY: vim, htop, `tail -f`, background jobs and ssh all
work, because nothing is being simulated. Layered on top is a clickable
breadcrumb, a live folder panel, saved commands and context menus, all of which
work by typing into that same shell.

---

## Contents

- [The guided tour](#the-guided-tour)
- [How it fits together](#how-it-fits-together)
- [Design notes](#design-notes)
- [Shells that outlive the browser, and Shellside](#shells-that-outlive-the-browser-and-shellside)
- [Drag and drop: upload and download](#drag-and-drop-upload-and-download)
- [On a phone](#on-a-phone)
- [The command assistant](#the-command-assistant)
- [Adding things without touching code](#adding-things-without-touching-code)
- [Services](#services)
- [Setup and the command line](#setup-and-the-command-line)
- [Access control](#access-control)
- [Reaching it from the internet](#reaching-it-from-the-internet)
- [Opening files](#opening-files)
- [Updating, and backing up](#updating-and-backing-up)
- [Where your data lives](#where-your-data-lives)
- [Layout](#layout)
- [Known gaps](#known-gaps)

---

## The guided tour

The first time a device opens Shellside, a short tour walks through the
interface one element at a time: terminal, tabs, path, files, menus, drag and
drop, saved commands, the assistant, settings. A phone gets its own steps (the
☰ drawer, the key bar). It is remembered per browser, and **Guide** in the
top bar shows it again.

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

## Design notes

### cwd comes from `/proc`, never from parsing stdin

Watching the `cd` commands as they are typed cannot be made to work. Shellside
reads `/proc/<foreground-pgid>/cwd` — the kernel's own
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

### Injection refuses to type into a running program

"Write into the PTY's stdin as if typed" is right, but if vim owns the terminal
then injecting `rm -rf {file_path}` types that *into vim*. Before injecting,
the backend checks that the shell itself holds the foreground process group:

    during `sleep 3` → inject blocked: "Terminal is busy running sleep. Exit it first."

Both this and the Ctrl-U line-clear that precedes a paste are switchable in
**Settings › General**.

### Substituted values are shell-quoted

A template such as `unzip {file_name} -d {cwd}`, breaks on
`my archive.zip`. Worse, for a file named `a b; rm -rf ~`:

    unquoted →  rm -rf /tmp/a b; rm -rf ~      ← deletes your home directory
    quoted   →  rm -rf '/tmp/a b; rm -rf ~'    ← what you meant

Quoting is on by default; a manifest can opt out with `"quote": false` when it
genuinely wants globbing or word splitting.

### The `|command|` convention needed a disambiguation rule

Pipes are shell syntax, so `|ps aux | grep ssh|` is ambiguous. The rule:

- a line that **starts and ends** with a pipe is one command, inner pipes included
- otherwise pipe pairs match non-greedily, for inline mentions

So `|ps aux | grep -i ssh|` becomes a single correct chip, `Run |ls -la| now.`
chips just the command, and `a | b` in prose chips nothing. The system prompt in
each LLM service tells the model to put commands alone on their line, which
keeps it in the unambiguous case.

### Vendored xterm, and sessions that survive a dropped connection

xterm.js is served from `web/vendor/` rather than a CDN, so the app opens on a
network that blocks everything except your tunnel.

PTY sessions outlive their websocket. A phone that drops its connection
reconnects to the same shell and gets its scrollback replayed, which on a phone
is the difference between usable and not. See *Shells that outlive the
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
  `auth/assist_key` in the data folder. Loopback alone would not do: behind a tunnel,
  internet traffic arrives from 127.0.0.1 too.
- Suggestions are printed as OSC 8 hyperlinks with a `shellside-paste:` URI;
  the browser terminal turns a click on one into a paste. Being real output,
  they are in the scrollback after a reconnect.

Limit: bash parses the line before the assistant sees it, so a request with an
unbalanced quote (`what's in here`) leaves bash waiting for the closing
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
Each one is written to `commands/<id>.json` in the [data folder](#where-your-data-lives).

A new context-menu action, in full:

    app/context_menu/file_entry/checksum/
      manifest.json    {"name": "Checksum", "icon": "#", "order": 55,
                        "match": {"extensions": [".iso", ".img"]}}
      command.txt      sha256sum {file_path}

Your own actions go in the data folder (`~/.config/shellside/context_menu/…`),
which an update never touches; the ones under `app/` ship with the code. A
folder of yours with the same id as a shipped one takes its place. Reload from
**Settings › Reload config from disk**, or just navigate — the mtime scan picks
it up.

**Placeholders:** `{file_name}` `{file_path}` `{cwd}` `{target_path}`
`{selection}` `{home}`

**Manifest keys:** `name` `description` `icon` `order` `enabled` `style`
(`default|danger|warning|info`) `bold` `confirm` `confirm_message` `quote`
`execute` `match.extensions` `match.kinds` `needs_picker` `picker_title`

`bold: true` marks a default action; the menu renders those first and rules a
line under them.

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

`echo` is an offline stub that returns a canned answer containing both a plain
and a piped command, so the whole Ask-the-model round trip can be exercised
without a model installed. Switch services in **Settings › Model**.

Adding a backend is adding a folder and repointing `active.json`. No core file
changes, ever.

---

## Setup and the command line

`bash setup.sh` builds the virtualenv and then runs `./shellside setup`, which
asks:

1. **Who can open it:** only this machine (`127.0.0.1`), or every device on the
   local network (`0.0.0.0`).
2. **Port:** 8022 by default.
3. **Start at boot:** writes `~/.config/systemd/user/shellside.service` for this
   checkout and runs `loginctl enable-linger`, so it starts at boot without
   anyone logged in. No sudo; if lingering cannot be enabled without it, setup
   prints the one command to run.
4. **A tunnel or reverse proxy:** turns on [tunnel mode](#how-trust-works).

Every answer can be given as an option instead (`--local`/`--lan`, `--port`,
`--autostart`/`--no-autostart`, `--tunnel`/`--no-tunnel`, `--yes` for the
defaults), for scripted installs. Run it again at any time to change them.

| command | what it does |
|---|---|
| `./shellside` | start it by hand (`--host`, `--port` override the saved values for one run) |
| `./shellside setup` | the questions above |
| `./shellside status` | address, port, password, tunnel mode, autostart, running or not |
| `./shellside link` | the sign-in link for a browser on this machine ([below](#if-you-lock-yourself-out)) |
| `./shellside reset-auth` | back to the unconfigured state ([below](#the-setup-password-and-starting-over)) |

These run in a shell on the machine, as the user Shellside runs as: at the
keyboard, over SSH, or in a Shellside terminal you are already signed in to.
Nothing on the web side can call them. Being able to run them already means
having that user's access, which is also all it takes to edit the files they
change.

As a service, the logs are in `journalctl --user -u shellside -f`.

---

## Access control

**Settings › Access.** A LAN password, an internet password for enrolling
devices from outside, real sessions, and a way back in if you ever lock
yourself out.

The gate is core code, not a plugin (`server/gate.py`, `server/auth.py`),
wired into every request, HTTP and websocket alike, before any route runs.
There is no setting that disables it, and nothing that makes it fall back to
*allow* when something fails to load: an earlier design did exactly that, and a
typo was enough to switch every check off silently.

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
`X-Forwarded-Host`, list the public address in `settings.json` in the data folder:

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

That still leaves a forwarder that adds no headers (`socat`, a raw TCP tunnel)
reached as `localhost`. If Shellside is reachable through any tunnel, turn on
**tunnel mode**: answer yes to the tunnel question in `./shellside setup`, or
switch it in Settings › Access; setting an **internet password** turns it on
too. Then loopback trust is off altogether and every browser needs a session,
including one on the machine itself (it signs in once with
`./shellside link`), as a second layer behind whatever protects the tunnel.

Cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` whenever the browser
reached Shellside over HTTPS.

A denied HTTP page load redirects to `/auth/login`; a denied API call gets a
plain 401; a refused cross-site request gets 403. A denied websocket is
closed at the handshake (4401 not signed in, 4403 cross-site), before the
PTY, the folder listing, or anything else it touches ever runs.

### The LAN password

Chosen on the first sign-in after setup (see below), changed from
**Settings › Access**. Hashed with `hashlib.scrypt` (stdlib, no new
dependency), so a forgotten password is a reset, not something anyone with
read access to the `auth/` folder could read back directly.

**Nothing listens beyond loopback unless something protects it.** At every
start, `run.py` binds `0.0.0.0` only when a LAN password is set, a setup
password is still valid, or "Allow the LAN with no password" is on. Otherwise
it binds `127.0.0.1`, whatever `settings.json` or `--host` say, and prints why. "Allow the LAN with no password" is a distinct, explicit
toggle for households that want that; it's not something an empty text field
falls into by accident.

Changing the password, or pressing **Sign out every device**, bumps a session
epoch that invalidates every other signed-in browser immediately. The one
exception: a session from the recovery link below is exempt from that epoch
on purpose, since it's the thing you'd use to fix a lockout in the first
place.

### The setup password, and starting over

A headless machine has no browser of its own, so the first sign-in has to
come from another device. When setup opens Shellside to the LAN and no
password exists yet, it prints a **setup password**:

- 8 characters, without look-alikes (`0 O 1 l I`), stored only as a hash;
- valid for 30 minutes, for one sign-in, from a LAN address only: never from
  loopback, never through a proxy or tunnel, never as an internet password;
- ignored, and refused like any wrong password, once a real LAN password
  exists; running setup again never replaces or weakens one.

Signing in with it leads to one page only: choose the real LAN password.
That session opens nothing else, and once the password is saved the setup
password is gone. If it expires unused, run `./shellside setup` again for a
new one.

Removing the LAN password in Settings › Access, or running
`./shellside reset-auth`, returns to the unconfigured state: no password,
"LAN with no password" off, every browser signed out, and `127.0.0.1` only
from the next start. Devices enrolled with an internet password are kept;
revoke them in Settings.

### If you lock yourself out

A fresh recovery link is generated every boot. Started by hand, Shellside
prints it in its terminal; as a service it keeps it out of the journal, and
`./shellside link` prints it (`--open` also opens it):

    Shellside is up. Signed out? This link signs you back in from this
    machine, no password:
        http://127.0.0.1:8022/auth/local?token=...

It works from this machine only (no proxy headers, a loopback `Host`), is not
consumed on use — losing that browser tab
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

---

## Reaching it from the internet

Keep Shellside on loopback and put a tunnel in front of it, for example a
Cloudflare tunnel:

    ./shellside setup --local --tunnel        # or answer the questions
    cloudflared tunnel --url http://127.0.0.1:8022

Then:

- **Protect the tunnel itself.** Put the hostname behind an access policy
  (Cloudflare Zero Trust Access, or your proxy's own login). Shellside's login
  is the second layer, not the only one.
- **Tunnel mode on**, which the `--tunnel` answer does: requests arriving
  through the tunnel come from `127.0.0.1`, and must not be trusted for it.
- **Devices sign in with an internet password** (Settings › Access): it has a
  number of uses and an expiry, and each device that uses it gets its own
  rotating credential, which you can revoke one by one. The LAN password never
  works through a tunnel.
- If the browser's address is not what reaches Shellside as `Host` or
  `X-Forwarded-Host`, add it to `allowed_origins` (see
  [How trust works](#how-trust-works)).

**Never** expose the port directly to the internet with a port forward on your
router: there is no TLS, and a shell is the most valuable thing on the machine.

---

## Opening files

### File handlers

**Settings › File types** holds one ordered list of *handlers*. A handler is a
name, a group of extensions, and one of three kinds:

| kind      | what a click does                                          |
| --------- | ---------------------------------------------------------- |
| `browser` | opens the file in a new tab, served from `/api/file`       |
| `editor`  | opens it in the built-in text editor                       |
| `command` | types `command` into the terminal                          |

`command` handlers also say whether they **run** at once or
**paste** and wait for Enter. Placeholders: `{file_path}` `{file_name}`
`{file_dir}` `{cwd}`, each shell-quoted.

That one list decides three things:

- **Left click** — the first handler, top to bottom, that lists the extension
  and is set to *Left click*. Move a handler up to make it win. A file no
  handler takes opens in the text editor, read-only if it is not text (so
  saving cannot corrupt it) and not at all over 5 MB.
- **Right click** — every handler that lists the extension, the left-click one
  first and in bold, then the generic actions (download, rename, move…). So the
  left click is always also a menu entry; a folder's menu starts with *Open*.
- **Visibility** — a file shows in the folder panel when any handler lists its
  extension. Folders and executables always show; dotfiles follow their switch.

Stored as `handlers` in `settings.json` in the data folder. A settings file from before
handlers existed is converted the first time it is read: customised click
actions become their own handlers at the top, and any extension that was only
*visible* is added to *Open with system default*.

### Icons

The same tab has an **Icons** list: the emoji shown in front of each name in
the folder panel. A folder gets the folder icon; a file gets the icon of the
first group that lists its extension, otherwise the executable icon if it has
the execute bit, otherwise the default one. Groups can be added, edited,
reordered and restored to the shipped set. Stored as `file_icons` in
`settings.json`. Emoji need a colour emoji font on the machine whose browser
shows them (Noto Color Emoji on most Linux desktops; phones have one).

### Files the browser can render

Photos, music, video, PDFs, text and JSON open in a new tab, served from
`/api/file`. **HTTP Range requests work**, so you can scrub through a video and
Safari will play it — media needs `206 Partial Content` or it refuses.

    Range: bytes=1000000-1000999
    → 206, content-range: bytes 1000000-1000999/3000000

The limit is codecs, not plumbing: an `.mp4` containing H.265 will not play in
Chrome no matter how well it is served; use a `command` handler (or download
it) for those.

---

## Updating, and backing up

A checkout made with `git clone` updates the usual way:

    git pull
    systemctl --user restart shellside    # or stop and start ./shellside

Nothing you changed lives in the code folder, so a pull never conflicts with
your settings (see [Where your data lives](#where-your-data-lives)).

**Settings › Update / Backup** is the other way, made for a headless machine
administered from a phone. Import a zip of new code, review exactly what it
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

Downloads the code folder as a zip, in the shape it is handed over in. Everything is
included except `__pycache__`, `.pyc`, `.git` and `_pending_update/`, so it can
be large and takes a few seconds to build. It streams from a temporary file, so
project size is not limited by memory.

---

## Where your data lives

Everything Shellside writes lives in one folder outside the code:
`~/.config/shellside` (or `$XDG_CONFIG_HOME/shellside`; `$SHELLSIDE_HOME`
overrides both). The code folder can be pulled, replaced or deleted without
losing anything.

    settings.json      settings, including bind_host and port
    auth/              passwords (hashed), session key, devices; mode 0700
    commands/          saved commands, one JSON each
    context_menu/      your own menu actions, and edits to the shipped ones
    scripts/           your own top-bar scripts
    favorites/         favorite folders
    data/              reference files for the model's context
    run/               the supervisor's pid file

A new install is seeded from the defaults under `app/`. An install from before
this folder existed is copied over once, on its first start; the old files are
left where they were.

---

## Layout

    run.py           supervisor: starts the app, applies updates, restarts it
    shellside        launcher: runs run.py with the checkout's virtualenv
    setup.sh         builds the virtualenv, then runs the guided setup

    server/          FastAPI backend
      main.py          routes and websockets
      gate.py          the access gate, in front of every request
      auth.py          passwords, sessions, setup password, rate limits
      auth_routes.py   /auth/* and Settings › Access
      devices.py       devices enrolled through the internet password
      wizard.py        ./shellside setup, status, link, reset-auth
      datahome.py      where user data lives, first-run seeding and migration
      pty_session.py   PTY, cwd detection, idle guard, session registry
      fsview.py        directory listing and file classification
      components.py    filesystem-as-configuration discovery
      templating.py    placeholder substitution with quoting
      services.py      pluggable service loader
      assist.py        command assistant: prompt, answer parsing, shell tokens
      update_plan.py   update zips: analysis and the plan run.py applies
      shell/           what runs inside the shell: bashrc, assist_client.py,
                       tmux.conf

    app/             shipped defaults, all folder-driven
      scripts/         right side of the top bar
        settings_panel/tabs/    one folder per settings tab
      commands/        default saved commands (seed the data folder)
      context_menu/    one folder per surface, one per action
      favorites/       default paths.json
      data/            reference files for LLM context

    services/llm/    one folder per model connector
    web/             frontend; vendor/ holds xterm, js/tour.js the guided tour
    tests/           pytest

---

## Known gaps

- `Move`'s picker lists directories only; moving into a path you cannot browse
  to needs typing.
- The mtime scan is O(files under `app/`). Fine at this scale; swap in
  `watchfiles` if that tree ever grows large.
- No audit log of executed commands. Worth adding before this faces the
  internet.
- No TLS of its own: over plain LAN HTTP the password and the session cookie
  cross the network in clear. Use a tunnel or a reverse proxy with HTTPS where
  that matters.
- Settings edits cover the common fields; the rest of `settings.json` is
  hand-edited.
- Linux only: the working-directory detection reads `/proc`.
