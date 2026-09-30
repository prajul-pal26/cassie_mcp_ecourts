"""Configuration for the Pranjul production gateway.

All tunable knobs live here. No magic numbers in the rest of the code.

Phase 0 WAF measurement (2026-05-23, single production IP):
  Endpoint probed   : stateWebService.php (DC, cheap GET)
  Ladder            : 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 30, 40 RPS
  Rung duration     : 20-30 s
  Total requests    : 2,955 successful upstream calls
  Clean sustained   : 30 RPS x 30 s = 901 reqs, 0 WAF blocks, p95 147ms
  Trip point        : 40 RPS — WAF blocked within ~3 s (4 of 51 fires)
  Trip likely cause : aggregate window rule, not pure RPS (we'd accumulated
                      ~950 reqs in the preceding 60 s before the trip)
  CSV log           : tests/waf_threshold.csv
  Detailed log      : tests/waf_threshold.log

Production cap rationale:
  - 30 RPS is the highest sustained-rate datapoint with zero blocks
  - 10 RPS = 3x safety margin below the trip point
  - 10 RPS = 600 reqs/min, comfortably below any plausible Sucuri window rule
  - At 10 RPS the worst-case cold-cache wave (100 users x 5 calls = 500 reqs)
    completes in 50 s, which is comfortable inside the SSE progress UX.
  - Headroom to raise to 15-20 RPS later if Phase 4 metrics support it.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

# Project root = the cassie-gateway-fastapi/ folder (this file lives at
# app/core/config.py, so climb three parents).
ROOT = Path(__file__).resolve().parents[2]
# Override the data dir via env var so Docker/Fly can mount a persistent
# volume at e.g. /data without code changes. Falls back to repo-local
# ./data for local dev.
_data_dir_override = os.getenv("ECOURTS_DATA_DIR")
DATA_DIR = Path(_data_dir_override) if _data_dir_override else (ROOT / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DB_PATH = DATA_DIR / "cache.sqlite"
JOBS_DB_PATH = DATA_DIR / "jobs.sqlite"
RATEBUCKET_DB_PATH = DATA_DIR / "ratebucket.sqlite"


# --------------------------------------------------------------------------- #
# Rate limiter — derived from Phase 0 measurement                              #
# --------------------------------------------------------------------------- #

# Sustained requests per second to upstream. Measured ceiling ~30 rps;
# trip at 40 rps. 15 rps = 50% of measured-safe, still below any plausible
# per-minute Sucuri window rule (15 * 60 = 900 reqs/min, vs. typical
# Sucuri defaults of ~1000-2000 reqs/min). Validated against live upstream.
UPSTREAM_RPS_CAP = float(os.getenv("UPSTREAM_RPS_CAP", "15"))

# A short burst budget on top of the steady rate. The bucket allows up to
# BURST requests instantaneously, then refills at UPSTREAM_RPS_CAP per
# second. cap + burst is the true 1-second ceiling — keep cap+burst <= 25
# to stay comfortably below the measured 30 RPS safe threshold.
UPSTREAM_BURST = int(os.getenv("UPSTREAM_BURST", "5"))


# --------------------------------------------------------------------------- #
# Cache                                                                        #
# --------------------------------------------------------------------------- #

# TTL in seconds for fresh cache hits. Distinct buckets for different
# endpoint families.
CACHE_TTL_SEARCH = int(os.getenv("CACHE_TTL_SEARCH", str(30 * 60)))
CACHE_TTL_CASE_DETAIL = int(os.getenv("CACHE_TTL_CASE_DETAIL", str(2 * 3600)))
CACHE_TTL_DROPDOWNS = int(os.getenv("CACHE_TTL_DROPDOWNS", str(24 * 3600)))

# Stale-OK fallback horizon: how long after expiry can a stale entry still
# be returned when the breaker is open. 24h means "yesterday's data is
# acceptable when upstream is dead, but not week-old data."
CACHE_STALE_OK_HORIZON = int(os.getenv("CACHE_STALE_OK_HORIZON", str(24 * 3600)))

# Hard cap on cache size. LRU eviction beyond this.
CACHE_MAX_SIZE_MB = int(os.getenv("CACHE_MAX_SIZE_MB", "500"))


# --------------------------------------------------------------------------- #
# Circuit breaker                                                              #
# --------------------------------------------------------------------------- #

BREAKER_FAIL_MAX = int(os.getenv("BREAKER_FAIL_MAX", "5"))
BREAKER_RESET_TIMEOUT = int(os.getenv("BREAKER_RESET_TIMEOUT", "120"))
# A WAF block in particular keeps the breaker open longer because Sucuri's
# cooldown is well-known (~60 min). Override the generic reset on WAF.
BREAKER_WAF_RESET_TIMEOUT = int(os.getenv("BREAKER_WAF_RESET_TIMEOUT", "3900"))

# Hard wall-clock ceiling for a single logical EcourtFetchV4._call()
# invocation, INCLUDING any bootstrap() re-entry it triggers on a WAF hit.
# Root-cause fix for a real incident: the WAF-cooldown branch used to sleep
# 60s then call bootstrap(), which itself makes 3 more _call() invocations
# that can EACH hit WAF-405 again and recurse into bootstrap() again, with
# no depth limit and no total-time budget — a single stuck task (e.g. a
# single-bench HC search, which has exactly one task) could silently block
# well past the job-stream's 300s idle timeout (see reliability/jobs.py),
# causing a misleading "stream idle timeout" while the worker was still
# blocked. 90s leaves 3x headroom under that 300s ceiling for one task,
# while concurrent sibling tasks (FIND_EXECUTOR_MAX_WORKERS) keep a
# multi-task job's SSE stream alive regardless.
CALL_TOTAL_BUDGET_SECONDS = float(os.getenv("CALL_TOTAL_BUDGET_SECONDS", "90"))

# Structural (wall-clock-INDEPENDENT) cap on how many times a single call
# budget may re-enter bootstrap() from a WAF-405 branch. Belt-and-suspenders
# alongside CALL_TOTAL_BUDGET_SECONDS: the time budget alone only bounds
# recursion because a real time.sleep(60) consumes real wall-clock time —
# this counter bounds it structurally too, so correctness never depends
# on an environmental assumption about sleep() actually blocking.
WAF_MAX_REENTRIES = int(os.getenv("WAF_MAX_REENTRIES", "2"))


# --------------------------------------------------------------------------- #
# Coalescer                                                                    #
# --------------------------------------------------------------------------- #

# Max wall-clock seconds a coalesced waiter will block before giving up.
COALESCER_MAX_WAIT = float(os.getenv("COALESCER_MAX_WAIT", "120"))


# --------------------------------------------------------------------------- #
# Jobs                                                                         #
# --------------------------------------------------------------------------- #

# How long to keep finished job rows around.
JOB_RETENTION_SECONDS = int(os.getenv("JOB_RETENTION_SECONDS", str(2 * 3600)))

# Per-find-job worker count. Real ceiling is the rate bucket; this number
# just caps how many work items can be in-flight before queueing.
FIND_WORKER_PARALLELISM = int(os.getenv("FIND_WORKER_PARALLELISM", "4"))

# Process-wide thread pool for find-jobs. Sized for ~1000 concurrent jobs,
# all of which spend most of their time blocked on the rate bucket. The
# bucket is the real throttle; this number just caps how many jobs can be
# in-flight before queueing in the executor.
FIND_EXECUTOR_MAX_WORKERS = int(os.getenv("FIND_EXECUTOR_MAX_WORKERS", "256"))

# Phonetic variants generated per "Find My Case" search term.
# Each variant is now emitted only in title-case (case permutations were
# removed after live testing confirmed eCourts v4 is case-insensitive on
# pet_name). cap=4 is enough to cover the top transliteration alternatives
# without producing noise variants. Combined with year fan-out:
#   4 variants × ~8 courts × 1 mode × 1 year = 32 tasks (single-year)
#   4 variants × ~8 courts × 1 mode × 5 years = 160 tasks (±2 years)
# Both well within the 15 RPS budget and the 512 MB memory ceiling.
FIND_VARIANT_CAP = int(os.getenv("FIND_VARIANT_CAP", "4"))

# ── Selection (per-court quota) ──
# The old single FIND_MAX_RESULTS=250 trimmed the whole job to the top 250 by
# confidence. For a common name almost every row ties at one confidence, so the
# trim kept whichever court returned first and DROPPED the other six — a citizen
# was told their case did not exist. Selection is now per court:
#   eff_quota = max(MIN_PER_COURT, min(PER_COURT_QUOTA, MAX_SELECTED // n_courts))
# so no court is ever dropped, and the retained set stays bounded.
FIND_PER_COURT_QUOTA = int(os.getenv("FIND_PER_COURT_QUOTA", "50"))
FIND_MIN_PER_COURT = int(os.getenv("FIND_MIN_PER_COURT", "10"))
FIND_MAX_SELECTED = int(os.getenv("FIND_MAX_SELECTED", "1000"))
FIND_FACET_TOP_N = int(os.getenv("FIND_FACET_TOP_N", "50"))

# Per-TASK runaway guard (was FIND_MAX_RESULTS, the whole-job cap that dropped
# courts). Now purely a memory/CPU bound on one task's contribution: a "Singh"
# bench with 16k+ rows (the 2026-06-06 OOM) is capped here before scoring. When
# it fires, that task's establishments are flagged `truncated` so counts stay
# honest. Facet counts are taken BEFORE this cap, so total_matched is exact
# unless a single task exceeds this.
FIND_MAX_PER_TASK = int(os.getenv("FIND_MAX_PER_TASK", "2000"))
# Deprecated alias kept so an old env var / import doesn't crash; unused.
FIND_MAX_RESULTS = FIND_MAX_PER_TASK

# Whether GET /api/find/{jid} ships rows by default. 1 = legacy (snapshot
# carries partial_cases + result). Flip to 0 once no legacy client remains, so
# polling is progress+facets only (~2 KB vs ~1 MB).
FIND_DEFAULT_ROWS = int(os.getenv("FIND_DEFAULT_ROWS", "1"))
FIND_ROWS_PAGE_MAX = int(os.getenv("FIND_ROWS_PAGE_MAX", "100"))


# --------------------------------------------------------------------------- #
# HTTP / Flask                                                                 #
# --------------------------------------------------------------------------- #

GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8000"))
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")

# SSE keepalive: server emits a comment line every N seconds when there's
# no real event, so intermediate proxies don't drop the connection.
SSE_HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", str(DATA_DIR / "gateway.log"))


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def as_dict() -> dict:
    """Snapshot of all config (for /health debug)."""
    return {
        "upstream_rps_cap": UPSTREAM_RPS_CAP,
        "upstream_burst": UPSTREAM_BURST,
        "cache_ttl_search": CACHE_TTL_SEARCH,
        "cache_ttl_case_detail": CACHE_TTL_CASE_DETAIL,
        "cache_ttl_dropdowns": CACHE_TTL_DROPDOWNS,
        "cache_stale_ok_horizon": CACHE_STALE_OK_HORIZON,
        "cache_max_size_mb": CACHE_MAX_SIZE_MB,
        "breaker_fail_max": BREAKER_FAIL_MAX,
        "breaker_reset_timeout": BREAKER_RESET_TIMEOUT,
        "breaker_waf_reset_timeout": BREAKER_WAF_RESET_TIMEOUT,
        "call_total_budget_seconds": CALL_TOTAL_BUDGET_SECONDS,
        "waf_max_reentries": WAF_MAX_REENTRIES,
        "coalescer_max_wait": COALESCER_MAX_WAIT,
        "job_retention_seconds": JOB_RETENTION_SECONDS,
        "find_worker_parallelism": FIND_WORKER_PARALLELISM,
        "find_variant_cap": FIND_VARIANT_CAP,
        "find_executor_max_workers": FIND_EXECUTOR_MAX_WORKERS,
        "find_per_court_quota": FIND_PER_COURT_QUOTA,
        "find_min_per_court": FIND_MIN_PER_COURT,
        "find_max_selected": FIND_MAX_SELECTED,
        "find_max_per_task": FIND_MAX_PER_TASK,
        "find_default_rows": FIND_DEFAULT_ROWS,
        "sse_heartbeat_seconds": SSE_HEARTBEAT_SECONDS,
    }
