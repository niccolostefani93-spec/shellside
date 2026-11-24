"""Ollama connector. Implements ask(prompt, context) -> str.

No model is assumed: the one picked in Settings › Model is used, and when
none is picked the first installed model able to generate text is used.
"""
from __future__ import annotations

import httpx

_config: dict = {}
DEFAULT_BASE = "http://127.0.0.1:11434"


def configure(config: dict) -> None:
    global _config
    _config = config or {}


def _base(config: dict) -> str:
    return (config.get("base_url") or DEFAULT_BASE).rstrip("/")


def _build(prompt: str, context: str) -> str:
    if not context.strip():
        return prompt
    return f"{prompt}\n\nTerminal output:\n```\n{context.strip()}\n```"


async def _installed(client: httpx.AsyncClient, base: str) -> list[dict]:
    response = await client.get(f"{base}/api/tags")
    response.raise_for_status()
    return response.json().get("models", []) or []


def _can_generate(model: dict) -> bool:
    caps = model.get("capabilities")
    if caps is None:  # older Ollama: no capabilities field, guess by name
        return "embed" not in model.get("name", "").lower()
    return "completion" in caps


async def list_models(config: dict | None = None) -> list[str]:
    """Names of the installed models that can answer a prompt."""
    config = _config if config is None else config
    async with httpx.AsyncClient(timeout=10.0) as client:
        models = await _installed(client, _base(config))
    return [m["name"] for m in models if m.get("name") and _can_generate(m)]


async def ask(prompt: str, context: str = "") -> str:
    base = _base(_config)
    model = (_config.get("model") or "").strip()
    try:
        async with httpx.AsyncClient(timeout=float(_config.get("timeout", 120))) as client:
            if not model:
                names = [m["name"] for m in await _installed(client, base)
                         if m.get("name") and _can_generate(m)]
                if not names:
                    return (f"Ollama at {base} has no model installed. Install one with "
                            "`ollama pull <model>`, then pick it in Settings › Model.")
                model = names[0]
            payload = {
                "model": model,
                "system": _config.get("system", ""),
                "prompt": _build(prompt, context),
                "stream": False,
                "think": False,
            }
            response = await client.post(f"{base}/api/generate", json=payload)
            if response.status_code == 400 and "think" in response.text:
                # Models without a thinking mode reject the flag on some versions.
                payload.pop("think")
                response = await client.post(f"{base}/api/generate", json=payload)
            if response.status_code == 404:
                return (f"Ollama has no model called “{model}”. Pick an installed one "
                        "in Settings › Model, or install it with "
                        f"`ollama pull {model}`.")
            response.raise_for_status()
            return response.json().get("response", "").strip()
    except httpx.ConnectError:
        return (f"Could not reach Ollama at {base}. Start it with `ollama serve`, "
                "or pick another service in Settings › Model.")
    except httpx.HTTPStatusError as exc:
        return f"Ollama returned {exc.response.status_code}: {exc.response.text[:400]}"
    except httpx.HTTPError as exc:
        return f"Ollama request failed: {exc}"
