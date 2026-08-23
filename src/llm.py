"""Minimal OpenAI-compatible chat-completion client (Kompakt Phase 7).

The coordinator proxies assistant replies through the local Hermes API
server (127.0.0.1:8642) — never a public provider. The server owns the
conversation; the client only renders stored messages.

Failure policy: generation failure NEVER fails the user's message —
the message is already persisted; the endpoint degrades to an honest
assistant note (see routers/v1.py). Retries belong to the next user
message, not to silent background loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are the assistant in the user's personal Kompakt system. "
    "You are a thinking and discussion partner, not an autonomous agent. "
    "Respond directly and concisely. Do NOT include your reasoning "
    "process in the reply. Plain text only; no HTML or markdown tables."
)


@dataclass
class LlmConfig:
    base_url: str = "http://127.0.0.1:8642/v1"
    api_key: str | None = None
    model: str = "deepseek-v4"
    timeout_s: float = 90.0
    max_history: int = 20  # messages of context sent (user+assistant)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


async def chat_completion(
    cfg: LlmConfig,
    messages: list[dict[str, str]],
) -> str:
    """Return the assistant reply text. Raises httpx errors / RuntimeError
    on transport or schema failure — caller decides how to degrade."""
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": cfg.system_prompt}] + messages,
    }
    async with httpx.AsyncClient(timeout=cfg.timeout_s) as client:
        resp = await client.post(
            f"{cfg.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected completion shape: {exc}") from exc
    if not content or not content.strip():
        raise RuntimeError("empty completion content")
    return content.strip()
