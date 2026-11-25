# Contributing to Shellwalk

Thanks for taking the time. Bug reports, fixes and small improvements are all
welcome.

## Running from source

Shellwalk needs Linux (it spawns a real shell on a PTY) and Python 3.11 or
newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SHELLWALK_HOME=$(mktemp -d) .venv/bin/python run.py --host 127.0.0.1
```

Then open http://127.0.0.1:8022. Use `--port` if 8022 is taken.
`./setup.sh` does the same venv setup and checks that every dependency
imports.

### Where user data lives

Everything Shellwalk writes at runtime (settings, saved commands, favorites,
service choices, credentials, the supervisor's pid file) lives in one
directory, never in the code checkout. It is `$SHELLWALK_HOME` if set,
otherwise `$XDG_CONFIG_HOME/shellwalk`, otherwise `~/.config/shellwalk`
(see `server/datahome.py`).

For development, point `SHELLWALK_HOME` at a throwaway directory as shown
above, so you never touch the data of a Shellwalk you actually use.

## Running the tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```

The tests start real bash sessions, so they need bash and a PTY.
`tests/conftest.py` already sets `SHELLWALK_HOME` to a temporary directory for
every run. CI runs the suite on Python 3.11 to 3.14.

## Ground rules

- **Small, focused pull requests.** One fix or one feature per PR is much
  easier to review than a bundle. If you are planning something large, open an
  issue first so we can agree on the shape.
- **The access gate stays standard library only.** `server/auth.py`,
  `server/gate.py` and `server/devices.py` decide who gets a shell. They use
  only `hmac`, `hashlib`, `secrets`, `json` and friends, on purpose: the gate
  must never fail because a package is missing or half-installed. Please do
  not add third-party imports there. The reasoning is in the docstring at the
  top of `server/auth.py`.
- **Security fixes go through a private report first.** See
  [SECURITY.md](SECURITY.md).
- Add or update a test in `tests/` when you change behaviour, and keep the
  existing comment style: explain *why*, not *what*.

## Services (plugins)

Optional backends live under `services/<category>/<name>/`. Today the only
category is `llm`, which answers "Ask the model". Each service is a folder
with two files:

- `manifest.json`: `name`, `description`, and a `config` object of defaults
  (for example `base_url`, `model`, `timeout`, `system`).
- `service.py`: an optional `configure(config: dict)` that receives the
  manifest's `config` merged with the user's own choices, and
  `async def ask(prompt: str, context: str = "") -> str`.

`services/<category>/active.json` names the service a fresh install starts
with. The service a user picks in Settings, and any address or model they
enter, is stored in their data directory, not in `services/`. Adding a
backend means adding a folder: no core file changes. `services/llm/echo/` is
a minimal example, and `ollama/` and `openai_compatible/` are real ones. The
loader is `server/services.py`.

Services are not used for access control: the gate is wired in
unconditionally and cannot be swapped out.
