"""Per-court result selection + facet counting for Find My Case.

Why this exists
---------------
The old path capped the whole job at FIND_MAX_RESULTS (250): each task's rows
were appended to one flat list which was re-sorted by confidence and trimmed to
250 after every task. For a common party name almost every row ties at the same
confidence (measured: 1408/1427 rows at score 76 for "Kumar"), so the sort was a
no-op and `[:250]` kept whichever court's task finished first. Six of seven
courts were dropped from the result — a citizen was told their case did not
exist when it did.

This module replaces the global trim with a per-court quota so no court is ever
dropped, and counts facets over the FULL match set (before any trim) so the UI
can show honest totals. eCourts returns every match in one payload with no
upstream pagination, so `len(rows)` per task is exact: `total_matched` is a real
number, not a floor.

The Selector is deliberately pure: no SQLite, no network, no FastAPI. It is fed
already-scored, already-deduped rows and hands back the bounded selected set plus
facet counts. That keeps it unit-testable in isolation and keeps the worker loop
readable.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, Optional

# Row field names, centralised because the normalize layer picks specific ones
# and getting them wrong silently drops data (the reason _case_id was broken).
# Rows carry: cino (not cnr), type_name (not case_type), establishment_code
# (not court_code), establishment_name, case_year.
_UNKNOWN_GROUP = "__unknown__"


def group_key(case: dict) -> str:
    """The court a row belongs to, as a citizen would recognise it.

    establishment_code is 100% present on normalized rows and is the specific
    court (District & Sessions, Civil Judge Sr Div, ...) — not the complex the
    task queried, which bundles several establishments. Fall back through name
    then the task's court_code, never to empty.
    """
    return (
        _s(case.get("establishment_code"))
        or _s(case.get("establishment_name"))
        or _s(case.get("court_code"))
        or _UNKNOWN_GROUP
    )


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _cino_seq(case: dict) -> int:
    """The 6-digit serial inside a CNR (SSDD NNNNNN YYYY), higher = filed later.

    Used only as a deterministic tie-break so order is stable across runs
    instead of being task-arrival order. 100% present, zero I/O. Returns -1 when
    it cannot be parsed so a garbage cino sorts last rather than raising.
    """
    cino = _s(case.get("cino") or case.get("cnr"))
    if len(cino) >= 10:
        mid = cino[-10:-4]
        if mid.isdigit():
            return int(mid)
    return -1


def rank_key(case: dict) -> tuple:
    """Total order within and across groups: strongest, then newest, then a
    stable string tie-break so the result is byte-identical across runs.

    (-confidence, -year, -cino_seq, cino) — the plan's key. When confidence and
    year tie (the common case), newer filings lead, which is defensible to a
    user ('newest first') and, crucially, deterministic.
    """
    conf = case.get("match_confidence")
    conf = conf if isinstance(conf, (int, float)) else -1
    year = _s(case.get("case_year"))
    year_n = int(year) if year.isdigit() else -1
    return (-conf, -year_n, -_cino_seq(case), _s(case.get("cino") or case.get("cnr")))


class Selector:
    """Accumulates rows across tasks into a per-court-bounded selected set while
    counting facets over everything it sees.

    Usage (per job):
        sel = Selector(per_court_quota=50, min_per_court=10, max_selected=1000)
        for task_rows in ...:
            sel.observe(task_rows, truncated=task_was_capped)
        store.finish_ok(jid, sel.selected(), meta=sel.meta())
    """

    def __init__(
        self,
        *,
        per_court_quota: int,
        min_per_court: int,
        max_selected: int,
        facet_top_n: int = 50,
        rank_key_fn: Callable[[dict], tuple] = rank_key,
    ) -> None:
        self._quota = max(1, per_court_quota)
        self._min = max(1, min_per_court)
        self._max_selected = max(self._min, max_selected)
        self._facet_top_n = facet_top_n
        self._rank = rank_key_fn

        # group_key -> list[row], each list kept sorted-enough via bounded insert
        self._groups: dict[str, list[dict]] = {}
        # group_key -> True once any task feeding it was capped upstream
        self._group_truncated: dict[str, bool] = {}

        # Facet counters over the FULL match set (every row observed, pre-trim).
        self._f_est: Counter = Counter()
        self._f_est_name: dict[str, str] = {}
        self._f_type: Counter = Counter()
        self._f_year: Counter = Counter()

        self._total_matched = 0
        self._any_truncated = False

    # ── ingest ────────────────────────────────────────────────────────────

    def observe(self, rows: Iterable[dict], *, truncated: bool = False) -> None:
        """Fold one task's rows in. `truncated` means the task hit the per-task
        runaway cap, so every establishment it touched is under-counted and is
        flagged — we genuinely don't know which rows were lost."""
        rows = list(rows)
        if truncated:
            self._any_truncated = True

        # eff_quota shrinks as groups appear so a very wide district (many
        # courts) can't blow past max_selected. Recomputed only when a NEW group
        # shows up, and existing groups are re-trimmed at that moment.
        for r in rows:
            self._total_matched += 1
            gk = group_key(r)

            # facets over everything, before any quota
            self._f_est[gk] += 1
            name = _s(r.get("establishment_name"))
            if name and gk not in self._f_est_name:
                self._f_est_name[gk] = name
            t = _s(r.get("type_name")) or _s(r.get("case_type"))
            if t and not t.isdigit():
                self._f_type[t] += 1
            y = _s(r.get("case_year"))
            if y:
                self._f_year[y] += 1

            new_group = gk not in self._groups
            if new_group:
                self._groups[gk] = []
            if truncated:
                self._group_truncated[gk] = True

            if new_group:
                self._reflow()

            self._insert(gk, r)

    def _eff_quota(self) -> int:
        n = max(1, len(self._groups))
        return max(self._min, min(self._quota, self._max_selected // n))

    def _insert(self, gk: str, row: dict) -> None:
        bucket = self._groups[gk]
        q = self._eff_quota()
        if len(bucket) < q:
            bucket.append(row)
            return
        # Bucket full: replace the worst row if this one ranks better.
        worst_i = max(range(len(bucket)), key=lambda i: self._rank(bucket[i]))
        if self._rank(row) < self._rank(bucket[worst_i]):
            bucket[worst_i] = row

    def _reflow(self) -> None:
        """A new group appeared, so eff_quota may have dropped. Trim every
        over-quota group down to the best `q` by rank. Runs at most once per
        distinct group (<= n_groups times per job), so it is cheap."""
        q = self._eff_quota()
        for gk, bucket in self._groups.items():
            if len(bucket) > q:
                bucket.sort(key=self._rank)
                del bucket[q:]

    # ── output ────────────────────────────────────────────────────────────

    def selected(self) -> list[dict]:
        """The bounded, globally-ordered result set across all courts."""
        out: list[dict] = []
        for bucket in self._groups.values():
            out.extend(bucket)
        out.sort(key=self._rank)
        return out

    def total_matched(self) -> int:
        return self._total_matched

    def total_selected(self) -> int:
        return sum(len(b) for b in self._groups.values())

    def counts_exact(self) -> bool:
        return not self._any_truncated

    def facets(self) -> dict:
        """Facet counts over the full match set, sorted count-desc / key-asc,
        capped at facet_top_n per dimension. A 1-cardinality facet is still
        emitted; the client decides whether to render it."""
        return {
            "establishment": self._facet_list(
                self._f_est,
                label_of=lambda k: self._f_est_name.get(k, k),
                selected_of=lambda k: len(self._groups.get(k, [])),
                truncated_of=lambda k: self._group_truncated.get(k, False),
            ),
            "case_type": self._facet_list(self._f_type),
            "year": self._facet_list(self._f_year),
        }

    def _facet_list(
        self,
        counter: Counter,
        *,
        label_of: Optional[Callable[[str], str]] = None,
        selected_of: Optional[Callable[[str], int]] = None,
        truncated_of: Optional[Callable[[str], bool]] = None,
    ) -> list[dict]:
        items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for key, count in items[: self._facet_top_n]:
            entry: dict[str, Any] = {"key": key, "count": count}
            if label_of:
                entry["label"] = label_of(key)
            if selected_of:
                entry["selected"] = selected_of(key)
            if truncated_of:
                entry["truncated"] = truncated_of(key)
            out.append(entry)
        return out
