"""Turn a structured diff into a short 'what changed and likely intent' note.

Providers (env LISTINGWATCH_LLM):
  ollama (default)  local Ollama at OLLAMA_HOST (default http://127.0.0.1:11434), model LISTINGWATCH_MODEL
  openai            any OpenAI-compatible endpoint: OPENAI_API_KEY, OPENAI_BASE_URL, LISTINGWATCH_MODEL
  none              no model; notes are skipped
The note is interpretation, not fact; the HTML timeline labels it that way.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

SYSTEM = (
    "You are a mobile games market analyst. You get the factual diff between two App Store or Google Play "
    "listing snapshots of one game. Write at most 3 short sentences: what changed, then the most likely "
    "intent (for example a seasonal or live event, a new feature, monetization or pricing copy, an ASO keyword "
    "shift, a creative refresh, a routine bug-fix update). Use only the diff. If the intent is unclear, say so. "
    "Do not invent numbers, events or features that are not in the diff. Plain text, no lists, no markdown."
)


def provider() -> str:
    return os.environ.get("LISTINGWATCH_LLM", "ollama").lower()


def model_name() -> str:
    default = "qwen3.5:9b" if provider() == "ollama" else "gpt-4o-mini"
    return os.environ.get("LISTINGWATCH_MODEL", default)


def model_id() -> str:
    """provider:model, plus the Ollama digest prefix when available, for the run log."""
    label = f"{provider()}:{model_name()}"
    if provider() == "ollama":
        try:
            host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
            with urllib.request.urlopen(f"{host}/api/tags", timeout=10) as resp:
                for m in json.loads(resp.read()).get("models", []):
                    if m.get("name") == model_name():
                        label += f"@{m.get('digest', '')[:12]}"
        except OSError:
            pass
    return label


def build_prompt(title: str, store: str, country: str, changes: list[dict]) -> str:
    slim = []
    for c in changes:
        c = dict(c)
        if "diff" in c:
            c["diff"] = [line[:300] for line in c["diff"][:30]]
        for key in ("added", "removed"):
            if key in c:
                c[key] = len(c[key])   # URLs are noise for the model; counts carry the signal
        slim.append(c)
    return f"Game: {title} ({store}, storefront {country.upper()})\nDiff:\n{json.dumps(slim, ensure_ascii=False, indent=1)}"


def _post(url: str, payload: dict, headers: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def complete(prompt: str, attempts: int = 3) -> str | None:
    """Call the model, retrying with backoff: a shared local Ollama is often busy."""
    for attempt in range(attempts):
        try:
            return _complete_once(prompt)
        except (OSError, TimeoutError) as exc:  # URLError and socket timeouts are OSErrors
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    return None


def _complete_once(prompt: str) -> str | None:
    p = provider()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    if p == "none":
        return None
    if p == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
        out = _post(f"{host}/api/chat", {"model": model_name(), "messages": messages, "stream": False,
                                          "think": False, "options": {"temperature": 0.2}}, {})
        return out["message"]["content"].strip()
    if p == "openai":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("LISTINGWATCH_LLM=openai needs OPENAI_API_KEY")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        out = _post(f"{base}/chat/completions", {"model": model_name(), "messages": messages, "temperature": 0.2},
                    {"Authorization": f"Bearer {key}"})
        return out["choices"][0]["message"]["content"].strip()
    raise ValueError(f"unknown LISTINGWATCH_LLM provider: {p}")
