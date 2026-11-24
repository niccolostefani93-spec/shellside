#!/usr/bin/env bash
# Build the virtualenv and install everything. Safe to re-run.
#
#     ./setup.sh                 # use the default python3
#     ./setup.sh python3.13      # use a specific interpreter
#
# pip is upgraded inside the fresh virtualenv before anything else is
# installed. An old pip is the usual reason a package lands half-written —
# present on disk, missing a subpackage — which then fails at request time
# rather than at install time.

set -euo pipefail
cd "$(dirname "$0")"

PY="${1:-python3}"
VENV=".venv"

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

# Verify rather than assume: importing is the only proof that counts.
echo "  Verifying the install"
"$VENV/bin/python" - <<'CHECK'
import importlib, sys

failures = []
for module in ("fastapi", "uvicorn", "multipart", "httpx",
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

cat <<EOF

  Done. Start it with:

      $VENV/bin/python run.py

  and open http://127.0.0.1:8022

  To reach it from another device on your network instead:

      $VENV/bin/python run.py --host 0.0.0.0

EOF
