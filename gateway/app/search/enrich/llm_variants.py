"""S1 — Gemini-augmented phonetic variants.

The library's rule-based `find_helpers.name_variants()` covers Latin-script
transliteration drift (Anil ↔ Aneel, Agarwal ↔ Aggarwal) well, but misses:
  - Nicknames (Raju ↔ Rajesh, Bunty ↔ Banti, Rinku ↔ Rinki)
  - Devanagari ↔ Roman ambiguities (अनिल ↔ Anil ↔ Aneel)
  - Caste / community spelling conventions
  - Common regional drift (Sastry ↔ Shastri, Krishna ↔ Krishnan)

This module asks Gemini Flash Lite once per unique name to suggest up to
12 variants. Results are cached indefinitely in a SQLite table keyed by
lowercased name — names don't change. The function never blocks a search:
on Gemini error, timeout, or missing API key, it returns an empty list and
the caller falls back to rule-based variants only.

Public function:
    augmented_variants(name, *, max_variants=12, timeout=4.0) -> list[str]

Cost: ~$0.0001 per unique name (Gemini Flash Lite pricing). Latency: ~400ms
first time, 0ms cache hit. Both safely amortised because names are reused
across users (popular names like "Sharma" / "Kumar" get cached once).

The result is intended to be UNIONED with rule-based variants in workers.py
(both sets dedup'd before fan-out). This module never replaces the rule
generator.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from app.core import config

log = logging.getLogger("gateway.llm_variants")


# ── Cache table (per-process SQLite, lazily created) ──────────────────────

_DB_LOCK = threading.Lock()
_DB_PATH = config.DATA_DIR / "llm_variants_cache.sqlite"


def _conn() -> sqlite3.Connection:
    """Open a connection (per-call, short-lived). The table is tiny so
    we don't need a connection pool. WAL is overkill for write-once."""
    c = sqlite3.connect(str(_DB_PATH), timeout=5.0, isolation_level=None)
    c.execute("""
        CREATE TABLE IF NOT EXISTS llm_variants (
            name_lower TEXT PRIMARY KEY,
            variants_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    return c


def _cache_get(name: str) -> Optional[list[str]]:
    key = name.strip().lower()
    if not key:
        return None
    with _DB_LOCK:
        try:
            c = _conn()
            row = c.execute(
                "SELECT variants_json FROM llm_variants WHERE name_lower = ?",
                (key,),
            ).fetchone()
            c.close()
        except sqlite3.Error as e:
            log.warning("llm cache read error: %r", e)
            return None
    if not row:
        return None
    try:
        data = json.loads(row[0])
        return [str(v) for v in data if isinstance(v, str)]
    except (ValueError, TypeError):
        return None


def _cache_put(name: str, variants: list[str]) -> None:
    key = name.strip().lower()
    if not key:
        return
    with _DB_LOCK:
        try:
            c = _conn()
            c.execute(
                "INSERT OR REPLACE INTO llm_variants(name_lower, variants_json, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(variants, ensure_ascii=False), time.time()),
            )
            c.close()
        except sqlite3.Error as e:
            log.warning("llm cache write error: %r", e)


# ── Gemini call ───────────────────────────────────────────────────────────

_GEMINI_ENDPOINT_TPL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/{model}:generateContent?key={key}"
)
# Flash-Lite is the cheapest variant; quality is plenty for this task.
_DEFAULT_MODEL = os.getenv("GEMINI_VARIANTS_MODEL", "gemini-2.0-flash-lite")

_PROMPT = (
    "You are a name-variation expert for Indian court records. Given a "
    "name, list up to {n} likely spelling and transliteration variants "
    "that might appear in eCourts (services.ecourts.gov.in) records.\n\n"
    "Include:\n"
    "- Common Latin transliteration drift (Anil/Aneel, Sastry/Shastri)\n"
    "- Nickname↔formal pairs (Raju↔Rajesh, Bunty↔Banti)\n"
    "- Doubled/single consonants (Agarwal/Aggarwal)\n"
    "- Vowel shifts (Sunil/Soonil, Kamla/Kamala)\n"
    "- Compound-name reorderings if applicable\n\n"
    "DO NOT include:\n"
    "- Totally unrelated names\n"
    "- Variants that change pronunciation drastically\n"
    "- More than {n} entries\n\n"
    'Return ONLY a JSON array of strings, no prose. Example for "Anil Agarwal":\n'
    '["Anil Agarwal","Anil Aggarwal","Aneel Agarwal","Aneel Aggarwal",'
    '"Anil Agrawal","Aneel Agrawal"]\n\n'
    'Name to vary: "{name}"\n'
    "JSON array:"
)


def _call_gemini(name: str, n: int, timeout: float) -> list[str]:
    """One-shot Gemini call. Returns [] on any error.

    Lazy-imports `requests` — already a dep but avoid module-load cost.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        log.debug("GEMINI_API_KEY not set; skipping LLM variants for %r", name)
        return []

    import requests  # already in requirements

    url = _GEMINI_ENDPOINT_TPL.format(model=_DEFAULT_MODEL, key=api_key)
    body = {
        "contents": [{
            "parts": [{"text": _PROMPT.format(n=n, name=name.strip())}]
        }],
        "generationConfig": {
            "temperature": 0.4,
            "topP": 0.9,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }

    try:
        r = requests.post(url, json=body, timeout=timeout)
    except requests.RequestException as e:
        log.warning("Gemini network error for %r: %r", name, e)
        return []

    if r.status_code != 200:
        log.warning("Gemini HTTP %d for %r: %s", r.status_code, name, r.text[:200])
        return []

    try:
        data = r.json()
    except ValueError:
        return []

    # Walk the standard Gemini response shape.
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        log.warning("Gemini response missing text for %r: %s", name, str(data)[:200])
        return []

    return _parse_variants_text(text, n=n)


def _parse_variants_text(text: str, *, n: int) -> list[str]:
    """Robustly extract a list of variant strings from Gemini's text output.

    Gemini with responseMimeType=application/json usually returns clean JSON,
    but defend against the occasional code-fence or leading explanation."""
    if not text:
        return []
    s = text.strip()
    # Strip ```json fences if present
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
    # Find the first '[' and matching last ']'
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1 or b <= a:
        return []
    blob = s[a:b + 1]
    try:
        arr = json.loads(blob)
    except ValueError:
        return []
    if not isinstance(arr, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in arr:
        if not isinstance(item, str):
            continue
        v = item.strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
        if len(out) >= n:
            break
    return out


# ── Public entrypoint ─────────────────────────────────────────────────────

def augmented_variants(name: Optional[str], *,
                       max_variants: int = 12,
                       timeout: float = 4.0) -> list[str]:
    """Return LLM-suggested variants for `name`. Cached indefinitely.

    Never raises; on any failure returns []. The caller MUST also keep the
    rule-based variants and union the two sets — this is purely additive.

    Args:
        name: Person/party name. Short (< 2 chars) or empty → returns [].
        max_variants: Upper bound on returned list length.
        timeout: HTTP timeout in seconds. Network errors are swallowed.
    """
    if not name or not isinstance(name, str):
        return []
    s = name.strip()
    if len(s) < 2:
        return []

    cached = _cache_get(s)
    if cached is not None:
        return cached[:max_variants]

    variants = _call_gemini(s, n=max_variants, timeout=timeout)
    # Cache even empty results so we don't keep retrying on every search —
    # rule-based generator is the fallback anyway.
    _cache_put(s, variants)
    return variants[:max_variants]


def merge_variants(rule_based: list[str], llm: list[str], *,
                   cap: int = 12) -> list[str]:
    """Union of rule-based + LLM variants, deduplicated case-insensitively,
    rule-based first (proven), capped at `cap`."""
    out: list[str] = []
    seen: set[str] = set()
    for v in (rule_based or []):
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
            if len(out) >= cap:
                return out
    for v in (llm or []):
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(v)
            if len(out) >= cap:
                return out
    return out
