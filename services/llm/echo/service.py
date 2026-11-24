"""Offline stub connector.

Exists so the whole Ask-the-model round trip - modal, request, chip parsing,
click-to-paste - can be exercised on a machine with no model installed.
"""
from __future__ import annotations


def configure(config: dict) -> None:
    pass


async def ask(prompt: str, context: str = "") -> str:
    head = prompt.strip().splitlines()[0][:80] if prompt.strip() else "your question"
    return (
        f"Offline stub answering: {head}\n\n"
        "No model is connected, so this is a placeholder. Pick a real service in "
        "Settings \u203a Model.\n\n"
        "|ls -la|\n\n"
        "And one containing a pipe, to prove the chip parser handles it:\n\n"
        "|ps aux | grep -i ssh|\n"
    )
