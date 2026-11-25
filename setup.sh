#!/usr/bin/env bash
# Install Shellwalk: build the virtualenv, then run the guided setup.
# Safe to re-run.
#
#     ./setup.sh                 # use the default python3
#     ./setup.sh python3.13      # use a specific interpreter
#     ./setup.sh --rebuild       # throw the virtualenv away and build it again
#
# A virtualenv that already works is kept; a broken or missing one is rebuilt.
#
# pip is upgraded inside the fresh virtualenv before anything else is
# installed. An old pip is the usual reason a package lands half-written —
# present on disk, missing a subpackage — which then fails at request time
# rather than at install time.

set -euo pipefail
cd "$(dirname "$0")"

REBUILD=0
PY="python3"
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=1 ;;
        -*) echo "  Unknown option: $arg" >&2; exit 2 ;;
        *) PY="$arg" ;;
    esac
done
VENV=".venv"

# Importing is the only proof that counts.
verify() {
    "$VENV/bin/python" - <<'CHECK'
import importlib, sys

failures = []
for module in ("fastapi", "uvicorn", "python_multipart", "httpx",
               "anyio._backends._asyncio", "starlette.staticfiles"):
    try:
        importlib.import_module(module)
    except Exception as exc:
        failures.append(f"    {module} -> {type(exc).__name__}: {exc}")

if failures:
    print("  Some packages did not install correctly:")
    print("\n".join(failures))
    sys.exit(1)
print("  All dependencies import cleanly.")
CHECK
}

# Run the guided setup only with a person at the keyboard. run.py's own
# auto-setup (a systemd start with broken packages) sets SHELLWALK_NO_WIZARD.
wizard() {
    if [ -z "${SHELLWALK_NO_WIZARD:-}" ] && [ -t 0 ] && [ -t 1 ]; then
        exec "$VENV/bin/python" run.py setup
    fi
    cat <<EOF2

  Installed. Configure it with:

      ./shellwalk setup

EOF2
    exit 0
}

if [ "$REBUILD" = 0 ] && [ -x "$VENV/bin/python" ] && \
   "$VENV/bin/python" -m pip install --quiet --no-cache-dir -r requirements.txt \
       >/dev/null 2>&1 && verify >/dev/null 2>&1; then
    echo "  The existing $VENV works, keeping it."
    wizard
fi

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "  '$PY' is not installed on this machine." >&2
    echo "  Available:" >&2
    for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        command -v "$candidate" >/dev/null 2>&1 && \
            echo "      $candidate  ($($candidate --version 2>&1))" >&2
    done
    exit 1
fi

echo "  Using $($PY --version 2>&1)"

# A virtualenv that failed half way through is worse than none at all.
if [ -d "$VENV" ]; then
    echo "  Removing the existing $VENV"
    rm -rf "$VENV"
fi

if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    echo "  Could not create the virtualenv. The venv module is packaged" >&2
    echo "  separately on Debian and Ubuntu:" >&2
    echo "      sudo apt install ${PY}-venv" >&2
    exit 1
fi

echo "  Upgrading pip inside the virtualenv"
"$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel

echo "  Installing dependencies"
"$VENV/bin/python" -m pip install --quiet --no-cache-dir -r requirements.txt

echo "  Verifying the install"
verify
wizard
