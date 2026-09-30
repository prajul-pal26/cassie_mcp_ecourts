"""Worker functions that drive jobs to completion via the reliability gateway.

These are the glue between:
  - jobs.JobStore (persistence)
  - reliability.GatewayCall (cache + coalesce + breaker + rate-limit)
  - legacy_v4_bridge.bridge_* (actual upstream calls)

Each worker is a pure function: given a job_id and the wired-up components,
it drives the job to a terminal status. Tests inject fake bridge functions;
production passes the real ones from legacy_v4_bridge.

The fan-out math for "Find My Case" is:
    courts × modes × variants  upstream calls
Each call is independently gated by the gateway, so:
  - identical (court, mode, variant) tuples from concurrent users coalesce
  - the rate bucket caps aggregate RPS regardless of fan-out width
  - cache makes repeat searches near-instant

Optional citizen-API enhancements (activated only when /api/find/smart
populates the relevant params; legacy /api/find behaviour unchanged):
  - `params['query']`: enables server-side match confidence scoring (E2).
  - `params['court_priority_hint']`: reorders task queue (S2).
  - `params['variants']` is unioned with LLM variants upstream (S1) — this
    module just consumes the final variants list.
  Each new_cases batch is sensitivity-classified (E6) and sorted by
  descending confidence (E2+E3) before being appended to partial_cases.
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.core import config
from app.reliability.gateway import (
    Breaker,
    Coalescer,
    GatewayCall,
    RateBucket,
    SqliteCache,
    UpstreamDown,
)
from app.reliability.jobs import JobStore
from app.search.selection import Selector

log = logging.getLogger("workers")


def _stable_key(prefix: str, body: dict) -> str:
    """Cache/coalescer key from a request body. Stable across runs."""
    s = json.dumps(body, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{h}"


def _case_id(case: dict) -> str:
    """Unique identifier for cross-task dedup.

    Normalized rows carry `cino` (not `cnr`/`CNR_no`), so the old key fell
    through to the composite for EVERY row — and that composite used field names
    the rows don't have (`case_type`->`type_name`, `court_code`->
    `establishment_code`), collapsing to "case_no||case_year|". Distinct cases
    that shared a case number across establishments deduped against each other
    and one silently vanished. cino is the real per-case id; the composite is a
    richer fallback for the rare row without one.
    """
    cino = case.get("cino") or case.get("cnr") or case.get("CNR_no")
    if cino:
        return str(cino)
    return "{}|{}|{}|{}".format(
        case.get("case_no", ""),
        case.get("type_name", "") or case.get("case_type", ""),
        case.get("case_year", ""),
        case.get("establishment_code", "") or case.get("court_code", ""),
    )


def _import_optional_helpers():
    """Lazy-import the new gateway helpers so workers.py degrades gracefully
    if the gateway package is missing (e.g., a partial deploy). Returns a
    tuple (score_case, sort_by_confidence, apply_sensitivity, rank_courts).
    Any missing helper is None and the caller skips that enhancement."""
    score_case = sort_by_confidence = apply_sensitivity = rank_courts = None
    try:
        from app.search.enrich.confidence import score_case, sort_by_confidence  # type: ignore
    except Exception as e:
        log.debug("confidence helpers unavailable: %r", e)
    try:
        from app.search.enrich.sensitivity import apply as apply_sensitivity  # type: ignore
    except Exception as e:
        log.debug("sensitivity helper unavailable: %r", e)
    try:
        from app.search.enrich.court_ranking import rank_courts  # type: ignore
    except Exception as e:
        log.debug("court_ranking helper unavailable: %r", e)
    return score_case, sort_by_confidence, apply_sensitivity, rank_courts


def run_find(*, store: JobStore, jid: str,
             cache: SqliteCache, coalescer: Coalescer,
             breaker: Breaker, bucket: RateBucket,
             bridge_funcs: dict,
             cache_ttl: float = 1800,
             stale_ok_horizon: float = 86400) -> None:
    """Drive a 'find' job to completion.

    Required params:
        search_term, state_code, district_code, courts[], modes[], variants[]

    Optional citizen-API params (set by /api/find/smart; legacy
    /api/find leaves them unset, preserving original behaviour):
        query: {party_name?, advocate_name?, year?, status?, case_type?}
            -> enables E2 confidence scoring per result
        court_priority_hint: 'civil' | 'criminal' | ...
            -> enables S2 court ranking

    `bridge_funcs` maps a mode name -> the bridge function to call. In
    production this is provided by production_app.py using legacy_v4_bridge.
    """
    row = store.get(jid)
    if row is None:
        log.warning("run_find: job %s not found", jid)
        return
    params = row["params"]

    store.mark_running(jid)

    courts = params.get("courts") or []
    modes = params.get("modes") or ["party"]
    variants = params.get("variants") or [params.get("search_term", "")]

    # Optional citizen-API context. Always-defined keys → simpler downstream.
    query_ctx = params.get("query") or {
        "party_name": params.get("search_term") if "party" in modes else None,
        "advocate_name": params.get("search_term") if "advocate" in modes else None,
    }
    priority_hint = params.get("court_priority_hint") or None

    # Lazy-load gateway helpers (each may be None if gateway pkg missing)
    score_case, sort_by_confidence, apply_sensitivity, rank_courts = \
        _import_optional_helpers()

    # ── S2: court ranking ──
    # Re-order courts so high-affinity ones run first. Same total work,
    # better perceived latency (citizen sees their match in seconds 1-3
    # instead of 20-30). No-op if rank_courts unavailable or hint missing.
    if rank_courts and priority_hint and courts:
        try:
            courts = rank_courts(courts, hint=priority_hint)
        except Exception as e:
            log.warning("rank_courts failed: %r — falling back to input order", e)

    # Year fan-out: eCourts v4 searchByPartyName REQUIRES a valid 4-digit
    # year. Sending year="" crashes the upstream with HTTP 500. Most Indian
    # citizens are searching for OLD cases (3-15 years old), so we expose
    # the year as a first-class field in the wizard and let the user opt
    # into progressive expansion.
    #
    # Search strategy (driven by `expand_strategy` from the frontend):
    #   - "single"  → just the user's year                       [1 year]
    #   - "pm1"     → year, year+1, year-1 (outward order)       [3 years]
    #   - "pm2"     → year, year+1, year-1, year+2, year-2       [5 years]
    #
    # The outward expansion order (center first, then alternating sides)
    # means results stream in for the user's exact year before broader
    # matches arrive. Best perceived latency.
    user_year = (
        params.get("year")
        or (params.get("query") or {}).get("year")
    )
    expand_strategy = (params.get("expand_strategy") or "single").strip().lower()

    def _build_year_list(center: int, strategy: str) -> list[str]:
        """year, year+1, year-1, year+2, year-2, ... (outward from center)."""
        if strategy == "single":
            return [str(center)]
        radius = 1 if strategy == "pm1" else 2 if strategy == "pm2" else 0
        out = [str(center)]
        for r in range(1, radius + 1):
            out.append(str(center + r))
            out.append(str(center - r))
        return out

    if user_year and str(user_year).strip() and str(user_year).strip() != "0":
        try:
            year_int = int(str(user_year).strip())
            years = _build_year_list(year_int, expand_strategy)
        except ValueError:
            years = [str(user_year).strip()]  # if non-numeric, pass as-is
    else:
        # Backward-compat fallback (frontend should always supply year):
        # default to current year span via FIND_DEFAULT_YEAR_SPAN env var.
        years = params.get("years")
        if not years:
            from datetime import datetime as _dt
            cy = _dt.now().year
            import os as _os
            try:
                span = max(1, int(_os.environ.get("FIND_DEFAULT_YEAR_SPAN", "1")))
            except ValueError:
                span = 1
            years = [str(cy - i) for i in range(span)]

    # Year fan-out applies only to modes whose v4 endpoint REQUIRES year
    # (currently `party`). Other modes (advocate) don't accept year and
    # would duplicate identical work N times if we fanned them out.
    YEAR_REQUIRED_MODES = {"party"}

    # Build the (court, mode, variant, year) task list
    tasks = []
    for court in courts:
        for mode in modes:
            for variant in variants:
                if mode not in bridge_funcs:
                    continue
                year_iter = years if mode in YEAR_REQUIRED_MODES else [None]
                for year in year_iter:
                    body = {
                        "state_code": params.get("state_code"),
                        # Per-task `district_code` overrides the job-level
                        # constant. The HC "search all benches" path uses
                        # this: each `court` entry carries its own
                        # district_code so the worker dispatches one
                        # search per bench. For DC and single-bench HC,
                        # `court.district_code` is absent and we fall
                        # back to the where.district_code the caller set.
                        "district_code": (
                            court.get("district_code")
                            or params.get("district_code")
                        ),
                        "court_code": court.get("court_code"),
                        "court_type": params.get("court_type", "dc"),
                    }
                    if year is not None:
                        body["year"] = year
                    # Per-mode body shape: pass the variant under the right key.
                    # Field names match what legacy_v4_bridge.bridge_* expects.
                    if mode == "party":
                        body["party_name"] = variant
                    elif mode == "advocate":
                        body["advocate_name"] = variant
                    else:
                        body["query"] = variant
                    tasks.append((mode, body, variant))

    total = len(tasks)
    completed = 0
    seen_ids: set = set()

    # Per-court selection + facet counting. Replaces the old flat all_cases +
    # global-trim, which dropped every court but the first to return.
    sel = Selector(
        per_court_quota=config.FIND_PER_COURT_QUOTA,
        min_per_court=config.FIND_MIN_PER_COURT,
        max_selected=config.FIND_MAX_SELECTED,
        facet_top_n=config.FIND_FACET_TOP_N,
    )

    # Per-task provenance accounting. Surfaced on the job snapshot as `meta`
    # so the API contract distinguishes "real empty result" from
    # "upstream was broken for all my tasks".
    sources_count = {"fresh": 0, "cache": 0, "stale": 0}
    failed_tasks = 0
    upstream_down_tasks = 0
    error_types: dict[str, int] = {}
    last_error_messages: list[str] = []

    def _build_meta() -> dict:
        return {
            "sources": dict(sources_count),
            "failed_tasks": failed_tasks,
            "upstream_down_tasks": upstream_down_tasks,
            "error_types": dict(error_types),
            "last_errors": list(last_error_messages[-3:]),
            "degraded": (failed_tasks > 0
                         or sources_count["stale"] > 0
                         or upstream_down_tasks > 0),
            "total_tasks": total,
            "successful_tasks": (sources_count["fresh"] + sources_count["cache"]
                                 + sources_count["stale"]),
            # Selection facets + honest totals over the FULL match set.
            "total_matched": sel.total_matched(),
            "total_selected": sel.total_selected(),
            "counts_exact": sel.counts_exact(),
            "facets": sel.facets(),
        }

    for mode, body, variant_used in tasks:
        # Cooperative cancellation
        if store.is_cancelled(jid):
            log.info("run_find: job %s cancelled at %d/%d", jid, completed, total)
            return

        key = _stable_key(f"find:{mode}", body)
        fn = bridge_funcs[mode]
        cases = []

        try:
            gw = GatewayCall(
                cache=cache, coalescer=coalescer, breaker=breaker,
                bucket=bucket, key=key, ttl=cache_ttl,
                stale_ok_horizon=stale_ok_horizon,
            )
            result = gw.run(lambda fn=fn, body=body: fn(body, proxies=None))
            cases = result.value or []
            sources_count[result.source] = sources_count.get(result.source, 0) + 1
            # [ECOURTS_DBG] — log per-task success result count + source
            # (fresh/cache/stale) so we can see if all tasks legitimately
            # returned empty vs cache returning [] vs upstream returning [].
            import os as _os
            if _os.environ.get("ECOURTS_DEBUG_LOG") == "1":
                log.warning(
                    "[ECOURTS_DBG] task OK jid=%s mode=%s court=%r variant=%r "
                    "cases=%d source=%s",
                    jid, mode, body.get("court_code"), variant_used,
                    len(cases), result.source,
                )
        except UpstreamDown:
            # Breaker open AND no cache for this key. Distinct from a
            # one-off task failure — track separately.
            upstream_down_tasks += 1
            failed_tasks += 1
            error_types["UpstreamDown"] = error_types.get("UpstreamDown", 0) + 1
            last_error_messages.append("upstream unavailable")
        except Exception as e:
            failed_tasks += 1
            err_type = type(e).__name__
            error_types[err_type] = error_types.get(err_type, 0) + 1
            msg = f"{err_type}: {str(e)[:120]}"
            last_error_messages.append(msg)
            log.warning("run_find: per-task failure mode=%s err=%r", mode, e)

        # Dedup new cases against what we already have
        new_cases = []
        for c in cases:
            cid = _case_id(c)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            new_cases.append(c)

        # ── E2: server-side confidence scoring ──
        # Run only when citizen-API supplied a query context. score_case
        # tolerates missing fields; we pass variant_used so the score
        # records provenance ("hit was via the 'Aneel' variant").
        if score_case and new_cases:
            try:
                new_cases = [
                    score_case(query_ctx, c, variant_used=variant_used)
                    for c in new_cases
                ]
            except Exception as e:
                log.warning("score_case failed mid-task: %r", e)

        # ── E6: sensitivity classification ──
        # apply_sensitivity sets sensitive_matter + sensitivity_category
        # on each case (mutates in place). Safe to call on either raw v4
        # rows or scored rows — it reads independent fields.
        if apply_sensitivity and new_cases:
            for c in new_cases:
                try:
                    apply_sensitivity(c)
                except Exception as e:
                    log.warning("sensitivity classify failed mid-task: %r", e)
                    break  # if one fails the rest probably will too

        # ── Runaway guard, per task ──
        # A common surname ("Singh") can match 10k+ rows on one bench; scoring
        # every one is the CPU cost, and an unbounded partial blob is the memory
        # cost that OOM'd the worker on 2026-06-06. Cap the rows this task
        # contributes to the selector; when it fires we don't know which
        # establishment lost rows, so mark the whole task truncated. Sort first
        # so the cap keeps the strongest.
        task_truncated = False
        if sort_by_confidence and new_cases and any(
            "match_confidence" in c for c in new_cases
        ):
            try:
                new_cases = sort_by_confidence(new_cases)
            except Exception as e:
                log.warning("sort_by_confidence failed mid-task: %r", e)
        if len(new_cases) > config.FIND_MAX_PER_TASK:
            new_cases = new_cases[:config.FIND_MAX_PER_TASK]
            task_truncated = True

        # ── Selection: per-court quota + facet counts (over the full set) ──
        sel.observe(new_cases, truncated=task_truncated)

        completed += 1
        # One atomic write: the bounded selected set + fresh meta. `partial` now
        # IS the selected set, so the terminal `done` writes the same thing and
        # nothing can vanish at the end.
        store.update_progress(
            jid, completed=completed, total=total,
            partial_cases_replace=sel.selected(),
            meta=_build_meta(),
        )

    # Final terminal write — only if not cancelled meanwhile
    if store.is_cancelled(jid):
        return

    final_meta = _build_meta()

    # If literally every task failed, this is NOT a "done with empty result"
    # — it's a real error. The plan's "no silent zero-result responses ever"
    # rule applies here.
    if total > 0 and failed_tasks >= total:
        primary_err = max(error_types, key=error_types.get) if error_types else "unknown"
        msg = (f"all {total} tasks failed ({primary_err})"
               + (f": {last_error_messages[-1]}" if last_error_messages else ""))
        store.finish_error(jid, msg, meta=final_meta)
        return

    # The selector already holds the bounded, globally-ranked set (its
    # selected() sorts by rank_key). `result` == the last `partial` we wrote,
    # so the client can never see the list shrink at `done`.
    store.finish_ok(jid, sel.selected(), meta=final_meta)
