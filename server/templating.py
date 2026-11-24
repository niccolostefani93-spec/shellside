"""Placeholder substitution for command.txt and saved-command entries.

Deviation from the handoff doc: substituted values are shell-quoted by default.
The doc's own example, `unzip {file_name} -d {cwd}`, breaks the moment a file is
called `my archive.zip`, and `rm -rf {file_path}` with an unquoted value is how
you delete the wrong thing. A manifest can opt out per action with
"quote": false when it deliberately wants word splitting or globbing.
"""
from __future__ import annotations

import re
import shlex
from typing import Any

PLACEHOLDER = re.compile(r"\{(\w+)\}")

KNOWN = ("file_name", "file_path", "cwd", "target_path", "selection", "home")


def render(template: str, ctx: dict[str, Any], quote: bool = True) -> tuple[str, list[str]]:
    """Return (rendered, missing_placeholders)."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in ctx or ctx[key] in (None, ""):
            missing.append(key)
            return match.group(0)
        value = str(ctx[key])
        return shlex.quote(value) if quote else value

    return PLACEHOLDER.sub(replace, template), missing


def placeholders(template: str) -> set[str]:
    return set(PLACEHOLDER.findall(template))
