"""Reliability stack — the 6-layer gateway for upstream calls.

Each upstream call flows through this pipeline:

   request
      |
      v
  +---+--- cache hit?     -> return                       (Layer 1: cache)
      |
      v
   coalesce identical concurrent requests                 (Layer 2: coalescer)
      |
      v
  +---+--- breaker open?  -> stale cache OR raise         (Layer 3: breaker)
      |
      v
   wait for rate-limit token                              (Layer 4: rate)
      |
      v
   call fetch_fn -> upstream                              (Layer 5: upstream)
      |
      v
   store result in cache, wake waiters                    (Layer 6: store)

Each component is independently testable and has no dependency on Flask.
This module is intentionally pure-Python with only stdlib + tiny SQLite I/O.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("reliability.gateway")


# --------------------------------------------------------------------------- #
# Exceptions                                                                   #
# --------------------------------------------------------------------------- #

class CircuitOpen(Exception):
    """Breaker is open; upstream is not being called."""


class UpstreamDown(Exception):
    """Breaker is open AND no usable cache is available."""


class WafBlocked(Exception):
    """A WAF block was detected. Treated specially by the breaker
    (longer reset)."""


class UpstreamFailure(Exception):
    """Base class for exceptions that reflect genuine upstream-health
    signals — these, and only these (plus WafBlocked), count toward the
    breaker's failure budget.

    This is the fix for a real, live-reproduced bug: Breaker.call() used to
    catch `BaseException` unconditionally, so a purely local bug (e.g. a
    ValueError from malformed input data, nothing to do with the upstream
    being reachable) could trip the breaker open and reject every OTHER
    request sharing that breaker instance for the full reset_timeout —
    even though the upstream was never actually down. Only exceptions
    raised by the transport layer (see app/upstream/ecourts_v4.py's
    _call()) as one of the subclasses below should ever reach here as
    something that counts against the breaker. Everything else must
    propagate as an ordinary, uncounted, task-level failure — "fail open"
    on our own bugs, matching the same philosophy documented in
    app/lib/rate-limit.ts on the Next.js side ("a bug in this utility must
    never take down the service").
    """


class UpstreamTimeout(UpstreamFailure):
    """The upstream call exceeded its timeout budget."""


class UpstreamConnectionError(UpstreamFailure):
    """A transport-level connection failure (DNS, refused, reset, etc.)."""


class UpstreamHTTPError(UpstreamFailure):
    """The upstream returned a non-2xx status after retries were exhausted."""

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        super().__init__(message or f"HTTP {status_code}")


# --------------------------------------------------------------------------- #
# Layer 1: SQLite-backed cache                                                 #
# --------------------------------------------------------------------------- #

class SqliteCache:
    """Persistent, thread-safe, TTL-aware cache.

    Stores arbitrary JSON-serialisable values. Uses WAL mode so reads
    never block writes. Eviction is LRU by access time, triggered on
    explicit `maybe_evict()` or implicitly when size_bytes exceeds the
    configured cap by a margin.
    """

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS cache (
            key       TEXT PRIMARY KEY,
            value     BLOB NOT NULL,
            expires_at REAL NOT NULL,
            stored_at  REAL NOT NULL,
            accessed_at REAL NOT NULL,
            size_bytes INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cache_accessed ON cache(accessed_at);
        CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
    """

    def __init__(self, path: str, default_ttl: int = 1800,
                 max_size_mb: int = 500):
        self.path = path
        self.default_ttl = default_ttl
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._lock = threading.Lock()  # serialises writes; reads are SQLite-level safe
        self._tlocal = threading.local()  # per-thread connection cache
        self._init_db()

    # ---- private ----
    def _conn(self) -> sqlite3.Connection:
        # Per-thread connection cache. SQLite connections aren't thread-safe,
        # so each thread gets its own. Reusing the connection avoids ~4 PRAGMA
        # roundtrips per call, which was the dominant cost under load (3600
        # update_progress calls in a 200-user test).
        c = getattr(self._tlocal, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10.0,
                                isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=10000;")
            c.execute("PRAGMA synchronous=NORMAL;")
            self._tlocal.conn = c
        return c

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Set WAL mode database-wide, once.
        boot = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        try:
            boot.execute("PRAGMA journal_mode=WAL;")
            boot.execute("PRAGMA synchronous=NORMAL;")
            boot.executescript(self._SCHEMA)
        finally:
            boot.close()

    # ---- public ----
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        if ttl is None:
            ttl = self.default_ttl
        blob = json.dumps(value, separators=(",", ":")).encode("utf-8")
        now = time.time()
        c = self._conn()
        with self._lock:
            c.execute(
                """INSERT OR REPLACE INTO cache
                   (key, value, expires_at, stored_at, accessed_at, size_bytes)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, blob, now + ttl, now, now, len(blob)),
            )

    def get(self, key: str) -> Optional[Any]:
        """Return value if present AND not expired; else None."""
        now = time.time()
        c = self._conn()
        row = c.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?",
            (key,)
        ).fetchone()
        if row is None:
            return None
        value_blob, expires_at = row
        if expires_at <= now:
            return None
        # Update accessed_at for LRU. Skip on contention to keep reads fast.
        try:
            c.execute("UPDATE cache SET accessed_at = ? WHERE key = ?",
                      (now, key))
        except sqlite3.OperationalError:
            pass
        return json.loads(value_blob)

    def get_stale_ok(self, key: str, horizon: float) -> Optional[Any]:
        """Return value if present AND not older than `horizon` seconds
        past its expiry; else None. Used as a circuit-open fallback."""
        now = time.time()
        c = self._conn()
        row = c.execute(
            "SELECT value, expires_at FROM cache WHERE key = ?",
            (key,)
        ).fetchone()
        if row is None:
            return None
        value_blob, expires_at = row
        if expires_at > now:
            return json.loads(value_blob)
        if (now - expires_at) > horizon:
            return None
        return json.loads(value_blob)

    def get_with_meta(self, key: str):
        """Like get(), but returns (value, stored_at) tuple or None.
        Used by GatewayCall to compute cache-age in the GatewayResult."""
        now = time.time()
        c = self._conn()
        row = c.execute(
            "SELECT value, expires_at, stored_at FROM cache WHERE key = ?",
            (key,)
        ).fetchone()
        if row is None:
            return None
        value_blob, expires_at, stored_at = row
        if expires_at <= now:
            return None
        try:
            c.execute("UPDATE cache SET accessed_at = ? WHERE key = ?",
                      (now, key))
        except sqlite3.OperationalError:
            pass
        return (json.loads(value_blob), stored_at)

    def get_stale_ok_with_meta(self, key: str, horizon: float):
        """Like get_stale_ok(), but returns (value, stored_at) tuple or None."""
        now = time.time()
        c = self._conn()
        row = c.execute(
            "SELECT value, expires_at, stored_at FROM cache WHERE key = ?",
            (key,)
        ).fetchone()
        if row is None:
            return None
        value_blob, expires_at, stored_at = row
        if expires_at > now:
            return (json.loads(value_blob), stored_at)
        if (now - expires_at) > horizon:
            return None
        return (json.loads(value_blob), stored_at)

    def delete(self, key: str) -> None:
        c = self._conn()
        with self._lock:
            c.execute("DELETE FROM cache WHERE key = ?", (key,))

    def clear(self) -> None:
        c = self._conn()
        with self._lock:
            c.execute("DELETE FROM cache")

    def size_bytes(self) -> int:
        c = self._conn()
        row = c.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM cache").fetchone()
        return int(row[0])

    def maybe_evict(self) -> int:
        """If over the size cap, evict oldest-accessed entries until under
        80% of the cap. Returns number of entries evicted."""
        total = self.size_bytes()
        if total <= self.max_size_bytes:
            return 0
        target = int(self.max_size_bytes * 0.8)
        evicted = 0
        c = self._conn()
        with self._lock:
            cur = c.execute(
                "SELECT key, size_bytes FROM cache ORDER BY accessed_at ASC"
            )
            keys_to_delete = []
            running = total
            for key, size in cur:
                if running <= target:
                    break
                keys_to_delete.append(key)
                running -= size
            for key in keys_to_delete:
                c.execute("DELETE FROM cache WHERE key = ?", (key,))
                evicted += 1
        return evicted


# --------------------------------------------------------------------------- #
# Layer 2: Coalescer (single-flight)                                           #
# --------------------------------------------------------------------------- #

@dataclass
class _InFlight:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None
    has_result: bool = False


class Coalescer:
    """Single-flight registry. If a fetch is already in-flight for the
    given key, secondary callers wait for it and receive the same result.

    Thread-safe. Uses one lock to manage the in-flight dict; the actual
    fetch_fn runs without holding the lock so it doesn't block other keys."""

    def __init__(self, max_wait: float = 120.0):
        self.max_wait = max_wait
        self._lock = threading.Lock()
        self._inflight: Dict[str, _InFlight] = {}

    def coalesce(self, key: str, fetch_fn: Callable[[], Any]) -> Any:
        with self._lock:
            entry = self._inflight.get(key)
            if entry is None:
                # First caller — register and run
                entry = _InFlight()
                self._inflight[key] = entry
                am_owner = True
            else:
                am_owner = False

        if am_owner:
            try:
                result = fetch_fn()
                entry.result = result
                entry.has_result = True
                return result
            except BaseException as e:
                entry.error = e
                raise
            finally:
                with self._lock:
                    # Pop BEFORE setting the event so secondary waiters
                    # who see the event-set but call a fresh coalesce()
                    # don't find a stale entry.
                    self._inflight.pop(key, None)
                entry.event.set()
        else:
            # Wait for the owner to finish
            got = entry.event.wait(timeout=self.max_wait)
            if not got:
                raise TimeoutError(f"coalesced wait for key={key!r} timed out")
            if entry.error is not None:
                raise entry.error
            return entry.result


# --------------------------------------------------------------------------- #
# Layer 3: Circuit breaker                                                     #
# --------------------------------------------------------------------------- #

class Breaker:
    """Three-state circuit breaker (closed / open / half_open).

    - In CLOSED: every call passes through; consecutive failures count
      toward `fail_max`. A success resets the counter.
    - On `fail_max` consecutive failures, opens. A `WafBlocked` failure
      opens immediately (no counter needed) AND uses `waf_reset_timeout`
      instead of the regular `reset_timeout`.
    - In OPEN: calls raise `CircuitOpen` without invoking the function.
    - After `reset_timeout` (or `waf_reset_timeout` for a WAF block),
      transitions to HALF_OPEN. The next call probes upstream;
      success closes, failure re-opens (and re-arms the timeout).
    """

    def __init__(self, fail_max: int = 5, reset_timeout: float = 120.0,
                 waf_reset_timeout: float = 3900.0):
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self.waf_reset_timeout = waf_reset_timeout
        self._lock = threading.Lock()
        self._state = "closed"
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._open_until: Optional[float] = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._compute_state_locked()

    def _compute_state_locked(self) -> str:
        if self._state == "open":
            if self._open_until is not None and time.time() >= self._open_until:
                self._state = "half_open"
        return self._state

    def call(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        with self._lock:
            state = self._compute_state_locked()
            if state == "open":
                raise CircuitOpen("breaker open")

        # Out of the lock — call fn freely
        try:
            result = fn(*args, **kwargs)
        except WafBlocked:
            with self._lock:
                self._state = "open"
                self._opened_at = time.time()
                self._open_until = self._opened_at + self.waf_reset_timeout
                self._failures = self.fail_max
            raise
        except UpstreamFailure:
            # Genuine upstream-health signal (timeout / connection error /
            # non-2xx after retries) — this is the only category, besides
            # WafBlocked above, allowed to count toward the failure budget.
            with self._lock:
                self._failures += 1
                if self._failures >= self.fail_max or self._state == "half_open":
                    self._state = "open"
                    self._opened_at = time.time()
                    self._open_until = self._opened_at + self.reset_timeout
            raise
        except BaseException as e:
            # Deliberately NOT counted toward breaker state. This is the
            # fix: anything that isn't a classified UpstreamFailure/
            # WafBlocked (a local bug, bad input, a KeyError on malformed
            # data, etc.) propagates as a normal task-level failure without
            # poisoning the shared breaker for every other caller. The log
            # line is an early-warning signal — if some genuinely-upstream
            # exception type shows up here repeatedly, it's a candidate to
            # reclassify as an UpstreamFailure subclass, and it's visible
            # before it causes an outage, not after.
            log.warning(
                "Breaker.call: non-upstream exception %s (%s) — not counted "
                "toward breaker failure budget",
                type(e).__name__, e,
            )
            raise
        else:
            with self._lock:
                self._failures = 0
                if self._state in ("half_open", "open"):
                    self._state = "closed"
                    self._opened_at = None
                    self._open_until = None
            return result


# --------------------------------------------------------------------------- #
# Layer 4: Rate bucket (token bucket)                                          #
# --------------------------------------------------------------------------- #

class RateBucket:
    """Standard token-bucket rate limiter.

    - `rps`: tokens refilled per second (sustained rate)
    - `burst`: max tokens held when idle
    - `acquire()`: blocks until a token is available
    - `try_acquire()`: returns False instead of blocking when empty
    - `try_acquire_or_wait(timeout)`: blocks up to `timeout` seconds

    Thread-safe via a single lock around the (tokens, last_refill) state.
    """

    def __init__(self, rps: float, burst: int = 1):
        if rps <= 0:
            raise ValueError("rps must be positive")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rps = float(rps)
        self.burst = int(burst)
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._last = time.time()
        self._cond = threading.Condition(self._lock)

    def _refill_locked(self) -> None:
        now = time.time()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rps)
            self._last = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def try_acquire_or_wait(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self._lock:
            while True:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                # How long until we'll have a token?
                needed = 1.0 - self._tokens
                wait_for = min(remaining, needed / self.rps)
                # Sleep without the lock to let other threads make progress
                # — but Condition.wait properly releases & re-acquires the
                # internal lock for us.
                self._cond.wait(timeout=wait_for)

    def acquire(self) -> None:
        """Block forever until a token is available."""
        while not self.try_acquire_or_wait(timeout=60.0):
            pass


# --------------------------------------------------------------------------- #
# Layer 5+6: GatewayCall — orchestrator                                        #
# --------------------------------------------------------------------------- #

@dataclass
class GatewayResult:
    """Result of a GatewayCall.run() — value plus provenance.

    `source` is one of:
      - "cache"  : fresh cache hit, no upstream call, no rate-token used
      - "fresh"  : upstream call succeeded just now
      - "stale"  : breaker open + within stale_ok_horizon; cached value
                   returned with degraded marker so callers can tell
    `age_seconds` is best-effort time since the underlying upstream call
    (0 for fresh, ~0 for cache, larger for stale).
    """
    value: Any
    source: str
    age_seconds: float = 0.0


@dataclass
class GatewayCall:
    """One pass through the full reliability stack.

    Build once per logical request and call .run(fetch_fn). The instance
    is intentionally cheap; create fresh per call to keep the key/ttl
    binding explicit at the call site.

    .run() returns a GatewayResult so callers can distinguish a cache hit
    from a stale fallback from a fresh upstream call — the "no silent
    zero-result responses" guarantee depends on the worker layer reading
    this and surfacing it to the API contract.
    """
    cache: SqliteCache
    coalescer: Coalescer
    breaker: Breaker
    bucket: RateBucket
    key: str
    ttl: Optional[float] = None
    stale_ok_horizon: float = 0.0

    def run(self, fetch_fn: Callable[[], Any]) -> GatewayResult:
        # Layer 1 — cache hit?
        fresh = self.cache.get_with_meta(self.key)
        if fresh is not None:
            value, stored_at = fresh
            return GatewayResult(value=value, source="cache",
                                 age_seconds=max(0.0, time.time() - stored_at))

        # Layers 2-6 happen inside coalescer so concurrent identical
        # callers share one execution.
        return self.coalescer.coalesce(self.key, lambda: self._fetch_path(fetch_fn))

    def _fetch_path(self, fetch_fn: Callable[[], Any]) -> GatewayResult:
        # Re-check cache — a previous coalesced caller may have populated
        # it while we were waiting at the coalescer lock.
        fresh = self.cache.get_with_meta(self.key)
        if fresh is not None:
            value, stored_at = fresh
            return GatewayResult(value=value, source="cache",
                                 age_seconds=max(0.0, time.time() - stored_at))

        # Layer 3 — breaker
        if self.breaker.state == "open":
            stale = self.cache.get_stale_ok_with_meta(self.key, self.stale_ok_horizon)
            if stale is not None:
                value, stored_at = stale
                return GatewayResult(value=value, source="stale",
                                     age_seconds=max(0.0, time.time() - stored_at))
            raise UpstreamDown("upstream circuit open and no cache available")

        # Layer 4 — rate
        self.bucket.acquire()

        # Layer 5 — upstream
        result = self.breaker.call(fetch_fn)

        # Layer 6 — store
        self.cache.set(self.key, result, ttl=self.ttl)
        return GatewayResult(value=result, source="fresh", age_seconds=0.0)
