"""Turn a structured diff into a short 'what changed and likely intent' note.

Provider (env LISTINGWATCH_LLM, default `auto`):
  auto      gemini if GEMINI_API_KEY is set, else deepseek if DEEPSEEK_API_KEY is set, else ollama
  gemini    Google Gemini through its OpenAI-compatible endpoint (GEMINI_API_KEY)
  deepseek  DeepSeek chat API (DEEPSEEK_API_KEY)
  openai    any OpenAI-compatible endpoint (OPENAI_API_KEY, OPENAI_BASE_URL)
  ollama    local Ollama at OLLAMA_HOST (default http://127.0.0.1:11434)
  none      no model; notes are skipped
LISTINGWATCH_MODEL overrides the model. The note is interpretation, not fact; the timeline labels it so.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

SYSTEM = (
    "You are a mobile games market analyst. You get the factual diff between two App Store or Google Play "
    "listing snapshots of one game. Write at most 3 short sentences: what changed, then the most likely "
    "intent (for example a seasonal or live event, a new feature, monetization or pricing copy, an ASO keyword "
    "shift, a creative refresh, a routine bug-fix update). Use only the diff. If the intent is unclear, say so. "
    "Do not invent numbers, events or features that are not in the diff. Plain text, no lists, no markdown."
)

# provider: (base URL, key env var, default model)
CLOUD = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-3.5-flash"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": (None, "OPENAI_API_KEY", "gpt-4o-mini"),
}

usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}


def provider() -> str:
    p = os.environ.get("LISTINGWATCH_LLM", "auto").lower()
    if p != "auto":
        return p
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "ollama"


def model_name() -> str:
    p = provider()
    default = CLOUD[p][2] if p in CLOUD else "qwen3.5:9b"
    return os.environ.get("LISTINGWATCH_MODEL", default)


def model_id() -> str:
    """provider:model, plus the Ollama digest prefix when available, recorded with each note and run."""
    label = f"{provider()}:{model_name()}"
    if provider() == "ollama":
        try:
            with urllib.request.urlopen(f"{_ollama_host()}/api/tags", timeout=10) as resp:
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


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _post(url: str, payload: dict, headers: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def complete(prompt: str, attempts: int = 4) -> str | None:
    """Call the model, retrying with backoff on rate limits (429), 5xx and network errors."""
    for attempt in range(attempts):
        try:
            return _complete_once(prompt)
        except urllib.error.HTTPError as exc:
            if (exc.code != 429 and exc.code < 500) or attempt == attempts - 1:
                raise
        except (OSError, TimeoutError):  # URLError and socket timeouts are OSErrors
            if attempt == attempts - 1:
                raise
        time.sleep(10 * (attempt + 1))
    return None


def _complete_once(prompt: str) -> str | None:
    p = provider()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    if p == "none":
        return None
    if p == "ollama":
        out = _post(f"{_ollama_host()}/api/chat", {"model": model_name(), "messages": messages, "stream": False,
                                                   "think": False, "options": {"temperature": 0.2}}, {}, timeout=180)
        usage["calls"] += 1
        usage["prompt_tokens"] += out.get("prompt_eval_count", 0)
        usage["completion_tokens"] += out.get("eval_count", 0)
        return out["message"]["content"].strip()
    if p not in CLOUD:
        raise ValueError(f"unknown LISTINGWATCH_LLM provider: {p}")
    base, key_var, _ = CLOUD[p]
    key = os.environ.get(key_var)
    if not key:
        raise RuntimeError(f"LISTINGWATCH_LLM={p} needs {key_var}")
    base = (base or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
    out = _post(f"{base}/chat/completions", {"model": model_name(), "messages": messages, "temperature": 0.2},
                {"Authorization": f"Bearer {key}"})
    usage["calls"] += 1
    usage["prompt_tokens"] += out.get("usage", {}).get("prompt_tokens", 0)
    usage["completion_tokens"] += out.get("usage", {}).get("completion_tokens", 0)
    return out["choices"][0]["message"]["content"].strip()
