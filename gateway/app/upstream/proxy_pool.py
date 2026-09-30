"""Webshare proxy pool for higher eCourts throughput + low latency.

eCourts' WAF is per-IP (~30 RPS/IP before blocking), so a single IP caps us at
~15 RPS. Spreading calls across many Webshare IPs raises the aggregate cap while
each IP stays under the WAF.

We use Webshare **static** IPs (`username-1`, `username-2`, … `username-N`) — each
suffix is a fixed exit IP, so a lane keeps a live connection to it (low latency),
unlike the rotating endpoint which re-handshakes every request.

Config (env; git-ignored .env or Fly secrets):
  WEBSHARE_PROXY_AUTH        = "user:pass"      (one account → N static IPs)
  WEBSHARE_PROXY_AUTH_2/_3.. = "user:pass"      (more accounts → more IPs)
  WEBSHARE_PROXY_AUTHS       = "u1:p1,u2:p2"    (comma list, alternative)
  WEBSHARE_IPS_PER_AUTH      = "10"             (IPs per account: -1 .. -N)
  WEBSHARE_PER_IP_RPS        = "15"             (safe RPS per IP)
  WEBSHARE_ENDPOINT          = "p.webshare.io:80"
  ECOURTS_PROXIES            = "http://u-1:p@host:port,..."  (explicit full URLs)

Runtime-added proxies (via POST /api/admin/proxies) are persisted to
`<DATA_DIR>/proxies.json` and merged into the pool. Add another account or
proxy and both the pool and the rate cap scale automatically. No auth set →
everything runs direct (single IP, 15 RPS) as before.
"""
from __future__ import annotations

import json
import os
import re
import threading

from app.core import config

_LOCK = threading.Lock()


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _endpoint() -> str:
    return _env("WEBSHARE_ENDPOINT", "p.webshare.io:80")


# ── configured accounts ──────────────────────────────────────────────────────

def parse_auths() -> list[str]:
    """All configured `user:pass` proxy auths, de-duplicated, in order."""
    auths: list[str] = []
    primary = _env("WEBSHARE_PROXY_AUTH")
    if primary:
        auths.append(primary)
    i = 2
    while True:
        v = _env(f"WEBSHARE_PROXY_AUTH_{i}")
        if not v:
            break
        auths.append(v)
        i += 1
    for part in _env("WEBSHARE_PROXY_AUTHS").split(","):
        part = part.strip()
        if part:
            auths.append(part)
    seen, out = set(), []
    for a in auths:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def ips_per_auth() -> int:
    try:
        return max(1, int(_env("WEBSHARE_IPS_PER_AUTH", "10")))
    except ValueError:
        return 10


def per_ip_rps() -> float:
    try:
        return max(1.0, float(_env("WEBSHARE_PER_IP_RPS", "15")))
    except ValueError:
        return 15.0


def _static_urls_for_auth(auth: str) -> list[str]:
    """`user:pass` → [http://user-1:pass@endpoint, … user-N] (N fixed IPs)."""
    user, _, pw = auth.partition(":")
    ep = _endpoint()
    return [f"http://{user}-{i}:{pw}@{ep}" for i in range(1, ips_per_auth() + 1)]


# ── explicit / runtime-added full proxy URLs ─────────────────────────────────

def _store_path():
    return config.DATA_DIR / "proxies.json"


def _load_runtime() -> list[str]:
    p = _store_path()
    try:
        if p.exists():
            data = json.loads(p.read_text())
            return [str(x) for x in data if x]
    except Exception:
        pass
    return []


def _save_runtime(urls: list[str]) -> None:
    try:
        _store_path().write_text(json.dumps(urls, indent=0))
    except Exception:
        pass


def env_proxy_urls() -> list[str]:
    return [u.strip() for u in _env("ECOURTS_PROXIES").split(",") if u.strip()]


def parse_proxy_input(raw: str) -> str:
    """Accept a full curl command, a `--proxy`/`-x` value, a proxy URL, or a
    bare `user:pass@host:port` — return a normalized `http://user:pass@host:port`."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty proxy")
    m = re.search(r'(?:--proxy|-x)\s+["\']?([^"\'\s]+)', raw)
    cand = m.group(1) if m else raw
    cand = cand.strip().strip('"').strip("'").rstrip("/")
    if "://" not in cand:
        cand = "http://" + cand
    if "@" not in cand or "." not in cand:
        raise ValueError(f"not a valid proxy url: {cand!r}")
    return cand


def add_proxy(raw: str) -> str:
    """Parse + persist a proxy. Returns the normalized URL. Caller rebuilds lanes."""
    url = parse_proxy_input(raw)
    with _LOCK:
        urls = _load_runtime()
        if url not in urls:
            urls.append(url)
            _save_runtime(urls)
    return url


def remove_proxy(url: str) -> bool:
    with _LOCK:
        urls = _load_runtime()
        if url in urls:
            urls.remove(url)
            _save_runtime(urls)
            return True
    return False


# ── the pool ─────────────────────────────────────────────────────────────────

def lane_proxy_urls() -> list[str]:
    """Every proxy lane URL (each a distinct fixed IP). Empty → run direct."""
    urls: list[str] = []
    for auth in parse_auths():
        urls.extend(_static_urls_for_auth(auth))
    urls.extend(env_proxy_urls())
    urls.extend(_load_runtime())
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def enabled() -> bool:
    return bool(lane_proxy_urls())


def effective_rps_cap(direct_default: float) -> float:
    """Global upstream RPS the rate bucket should allow = (#IPs × safe-RPS/IP).
    Direct (no proxy) → the caller's default."""
    n = len(lane_proxy_urls())
    if n == 0:
        return direct_default
    return n * per_ip_rps()


def _mask(url: str) -> str:
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)


def summary(reveal: bool = False) -> dict:
    """Pool state for /health (masked) or the admin GET (reveal=True → full URLs)."""
    lanes = lane_proxy_urls()
    return {
        "enabled": bool(lanes),
        "accounts": len(parse_auths()),
        "ips_per_auth": ips_per_auth(),
        "total_proxies": len(lanes),
        "per_ip_rps": per_ip_rps(),
        "effective_rps_cap": effective_rps_cap(0.0),
        "endpoint": _endpoint(),
        "runtime_added": len(_load_runtime()),
        "proxies": lanes if reveal else [_mask(u) for u in lanes],
    }
