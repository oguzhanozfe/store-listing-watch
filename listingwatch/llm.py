"""Turn a structured diff into a short 'what changed and likely intent' note.

Provider (env LISTINGWATCH_LLM, default `auto`):
  auto      gemini if GEMINI_API_KEY is set, else deepseek if DEEPSEEK_API_KEY is set, else ollama
  gemini    Google Gemini through its OpenAI-compatible endpoint (GEMINI_API_KEY)
  deepseek  DeepSeek chat API (DEEPSEEK_API_KEY)
  openai    any OpenAI-compatible endpoint (OPENAI_API_KEY, OPENAI_BASE_URL)
  ollama    local Ollama at OLLAMA_HOST (default http://127.0.0.1:11434)
  none      no model; notes are skipped
LISTINGWATCH_MODEL overrides the model; a comma-separated list is tried in order, moving to the next model on
a rate limit (429), overload (5xx) or network error, never looping on one model. The note is interpretation, not fact; the timeline labels it so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
    # Free-tier daily quotas are per model, so spread across several: Gemma first, Gemini flash variants after.
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY",
               "gemma-4-31b-it,gemini-3.5-flash-lite,gemma-4-26b-a4b-it,gemini-3.1-flash-lite,gemini-3.5-flash"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openai": (None, "OPENAI_API_KEY", "gpt-4o-mini"),
}

# Some models (Gemma via the Gemini API, Qwen) put reasoning inline; keep only the answer.
_REASONING = re.compile(r"<(think|thought)>.*?</\1>", re.S | re.I)

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


def models() -> list[str]:
    p = provider()
    default = CLOUD[p][2] if p in CLOUD else "qwen3.5:9b"
    return [m.strip() for m in os.environ.get("LISTINGWATCH_MODEL", default).split(",") if m.strip()]


def model_name() -> str:
    return models()[0]


def model_id(model: str | None = None) -> str:
    """provider:model, plus the Ollama digest prefix when available, recorded with each note and run."""
    model = model or model_name()
    label = f"{provider()}:{model}"
    if provider() == "ollama":
        try:
            with urllib.request.urlopen(f"{_ollama_host()}/api/tags", timeout=10) as resp:
                for m in json.loads(resp.read()).get("models", []):
                    if m.get("name") == model:
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


def ollama_ready() -> bool:
    """True when local Ollama answers and has the configured model."""
    try:
        with urllib.request.urlopen(f"{_ollama_host()}/api/tags", timeout=3) as resp:
            names = {m.get("name") for m in json.loads(resp.read()).get("models", [])}
    except (OSError, ValueError):
        return False
    return any(m in names or f"{m}:latest" in names for m in models())


def demo_mode() -> str:
    """'live' when a cloud key or a local Ollama model is available, else 'replay' (recorded notes)."""
    p = provider()
    if p in ("none", "replay"):
        return "replay"
    if p in CLOUD:
        return "live" if os.environ.get(CLOUD[p][1]) else "replay"
    return "live" if p == "ollama" and ollama_ready() else "replay"


def _post(url: str, payload: dict, headers: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def cache_key(prompt: str) -> str:
    """Hash of everything that shapes the answer, so identical diffs never cost a second call."""
    return hashlib.sha256(json.dumps([provider(), models(), SYSTEM, prompt]).encode()).hexdigest()


def complete(prompt: str) -> tuple[str, str] | None:
    """Try each configured model once. Returns (text, model id) or None when notes are off."""
    if provider() == "none":
        return None
    errors = []
    for model in models():
        try:
            return _REASONING.sub("", _complete_once(prompt, model)).strip(), model_id(model)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            errors.append(f"{model}: HTTP {exc.code}")
        except (OSError, TimeoutError) as exc:  # URLError and socket timeouts are OSErrors
            errors.append(f"{model}: {exc}")
    raise RuntimeError("all models failed: " + "; ".join(errors))


def _complete_once(prompt: str, model: str) -> str:
    p = provider()
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    if p == "ollama":
        out = _post(f"{_ollama_host()}/api/chat", {"model": model, "messages": messages, "stream": False,
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
    out = _post(f"{base}/chat/completions", {"model": model, "messages": messages, "temperature": 0.2},
                {"Authorization": f"Bearer {key}"})
    usage["calls"] += 1
    usage["prompt_tokens"] += out.get("usage", {}).get("prompt_tokens", 0)
    usage["completion_tokens"] += out.get("usage", {}).get("completion_tokens", 0)
    return out["choices"][0]["message"]["content"].strip()
