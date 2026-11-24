"""Chat-completions connector for any OpenAI-shaped endpoint."""
from __future__ import annotations

import os

import httpx

_config: dict = {}


def configure(config: dict) -> None:
    global _config
    _config = config or {}


def _headers(config: dict) -> dict:
    key = os.environ.get(config.get("api_key_env", ""), "")
    return {"Authorization": f"Bearer {key}"} if key else {}


async def list_models(config: dict | None = None) -> list[str]:
    """Model ids the endpoint advertises on GET /models."""
    config = _config if config is None else config
    base = config.get("base_url", "http://127.0.0.1:8000/v1").rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base}/models", headers=_headers(config))
        response.raise_for_status()
        return [m["id"] for m in response.json().get("data", []) if m.get("id")]


async def ask(prompt: str, context: str = "") -> str:
    base = _config.get("base_url", "http://127.0.0.1:8000/v1").rstrip("/")
    user = prompt if not context.strip() else (
        f"{prompt}\n\nTerminal output:\n```\n{context.strip()}\n```")
    headers = {}
    key = os.environ.get(_config.get("api_key_env", ""), "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": _config.get("model", "local"),
        "messages": [
            {"role": "system", "content": _config.get("system", "")},
            {"role": "user", "content": user},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=float(_config.get("timeout", 120))) as client:
            response = await client.post(f"{base}/chat/completions",
                                         json=payload, headers=headers)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
    except httpx.ConnectError:
        return f"Could not reach the model endpoint at {base}."
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        return f"Model request failed: {exc}"
