"""Async job system — SQLite-backed.

JobStore is the persistence layer for "Find My Case" jobs. It owns:
  - Submission and lifecycle (pending -> running -> done|error|cancelled|interrupted)
  - Per-establishment progress accumulation
  - SSE-friendly stream() generator
  - Crash recovery (jobs left "running" on init get marked "interrupted")
  - Retention/reaping of finished jobs

Worker functions (_run_find etc.) live in workers.py and call into the
reliability gateway for upstream work; this file is just storage.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Generator, Optional

_VALID_STATUSES = {"pending", "running", "done", "error", "cancelled", "interrupted"}


class JobStore:
    """SQLite-backed job state.

    Thread-safe: writes are serialised through one lock; reads use
    fresh connections (SQLite per-connection isolation is fine when
    we serialise writes ourselves)."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS jobs (
            id            TEXT PRIMARY KEY,
            kind          TEXT NOT NULL,
            params_json   TEXT NOT NULL,
            status        TEXT NOT NULL,
            completed     INTEGER NOT NULL DEFAULT 0,
            total         INTEGER NOT NULL DEFAULT 0,
            partial_json  TEXT NOT NULL DEFAULT '[]',
            result_json   TEXT,
            meta_json     TEXT,
            error         TEXT,
            created_at    REAL NOT NULL,
            updated_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);
    """
    _MIGRATIONS = [
        # Adds meta_json to pre-existing DBs that were created before this column existed.
        "ALTER TABLE jobs ADD COLUMN meta_json TEXT",
    ]

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._tlocal = threading.local()
        self._init_db()
        self._recover_orphans()

    # ---- private ----
    def _conn(self) -> sqlite3.Connection:
        # Per-thread connection cache — see SqliteCache._conn for the
        # reasoning. Drops per-call PRAGMA overhead and was the dominant
        # latency under 200-user load.
        c = getattr(self._tlocal, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10.0,
                                isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=10000;")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.row_factory = sqlite3.Row
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
            # Apply migrations one by one; ignore duplicate-column errors so
            # this is idempotent across upgrades.
            for stmt in self._MIGRATIONS:
                try:
                    boot.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        finally:
            boot.close()

    def _recover_orphans(self) -> None:
        """Any job in pending|running when we boot is from a crashed
        previous process. Mark as 'interrupted' so callers see a clean
        terminal state."""
        now = time.time()
        c = self._conn()
        with self._lock:
            c.execute(
                "UPDATE jobs SET status='interrupted', updated_at=? "
                "WHERE status IN ('pending','running')",
                (now,)
            )

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        try:
            meta = json.loads(row["meta_json"]) if row["meta_json"] else None
        except (KeyError, IndexError):
            # Older rows without the meta_json column (very old test DBs)
            meta = None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "params": json.loads(row["params_json"]),
            "status": row["status"],
            "completed": row["completed"],
            "total": row["total"],
            "partial_cases": json.loads(row["partial_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "meta": meta,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- public ----
    def submit(self, kind: str, params: dict) -> str:
        jid = uuid.uuid4().hex
        now = time.time()
        c = self._conn()
        with self._lock:
            c.execute(
                """INSERT INTO jobs
                   (id, kind, params_json, status, completed, total,
                    partial_json, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', 0, 0, '[]', ?, ?)""",
                (jid, kind, json.dumps(params), now, now),
            )
        return jid

    def get(self, jid: str) -> Optional[dict]:
        c = self._conn()
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def mark_running(self, jid: str) -> None:
        now = time.time()
        c = self._conn()
        with self._lock:
            c.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE id=?",
                (now, jid),
            )

    def update_progress(self, jid: str, completed: int, total: int,
                        partial_cases_append: Optional[list] = None,
                        partial_cases_replace: Optional[list] = None,
                        meta: Optional[dict] = None) -> None:
        """Atomically bump completed/total, set the partial rows, and (optionally)
        fold in meta — in ONE write.

        `partial_cases_replace` is the path callers should use: it does a bare
        json.dumps of a bounded list with NO read-modify-write. The old
        `partial_cases_append` path did SELECT -> json.loads -> extend ->
        json.dumps on every task, which is O(n^2) over a job and re-serialised a
        growing 1+ MB blob each time. It is kept only for backward compat and
        should not be used for new code.

        Folding `meta` in here (instead of a separate update_meta call) removes
        the second write per task and the race where stream() could read
        completed=N before the matching meta landed.
        """
        now = time.time()
        c = self._conn()
        with self._lock:
            if partial_cases_replace is not None:
                partial_json = json.dumps(partial_cases_replace)
            elif partial_cases_append:
                row = c.execute(
                    "SELECT partial_json FROM jobs WHERE id=?", (jid,)
                ).fetchone()
                if row is None:
                    return
                cur = json.loads(row["partial_json"])
                cur.extend(partial_cases_append)
                partial_json = json.dumps(cur)
            else:
                partial_json = None

            sets = ["completed=?", "total=?", "updated_at=?"]
            args: list = [completed, total, now]
            if partial_json is not None:
                sets.append("partial_json=?")
                args.append(partial_json)
            if meta is not None:
                sets.append("meta_json=?")
                args.append(json.dumps(meta))
            args.append(jid)
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)

    def get_progress(self, jid: str) -> Optional[dict]:
        """Progress + meta for a job, WITHOUT the row blobs.

        The single highest-leverage read in the streaming path: stream() polls
        this every 0.25s per connected client, and the old `get()` deserialised
        the full 1+ MB partial_json blob on every one of those calls. This
        selects only the small columns, so a 4 Hz poll costs nothing.
        """
        c = self._conn()
        row = c.execute(
            """SELECT id, kind, status, completed, total, meta_json, error,
                      created_at, updated_at
               FROM jobs WHERE id=?""",
            (jid,),
        ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(row["meta_json"]) if row["meta_json"] else None
        except (KeyError, IndexError):
            meta = None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "completed": row["completed"],
            "total": row["total"],
            "meta": meta,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_rows(self, jid: str) -> Optional[dict]:
        """Just the row blobs (partial + result), for the paginated /rows
        endpoint. Read at human tap-rate, not on the streaming loop."""
        c = self._conn()
        row = c.execute(
            "SELECT partial_json, result_json FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if row is None:
            return None
        return {
            "partial_cases": json.loads(row["partial_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def finish_ok(self, jid: str, result: Any,
                  meta: Optional[dict] = None) -> None:
        """Mark a job done. `meta` is optional execution metadata
        (failed_tasks, sources, errors_summary, degraded flag, etc.)
        that the API contract surfaces so callers can't mistake an empty
        result for a healthy zero-match query."""
        now = time.time()
        meta_json = json.dumps(meta) if meta is not None else None
        c = self._conn()
        with self._lock:
            c.execute(
                """UPDATE jobs SET status='done', result_json=?, meta_json=?,
                   updated_at=? WHERE id=?""",
                (json.dumps(result), meta_json, now, jid),
            )

    def finish_error(self, jid: str, error_msg: str,
                     meta: Optional[dict] = None) -> None:
        now = time.time()
        meta_json = json.dumps(meta) if meta is not None else None
        c = self._conn()
        with self._lock:
            c.execute(
                """UPDATE jobs SET status='error', error=?, meta_json=?,
                   updated_at=? WHERE id=?""",
                (error_msg, meta_json, now, jid),
            )

    def update_meta(self, jid: str, meta: dict) -> None:
        """Set/overwrite the running meta blob mid-flight."""
        c = self._conn()
        with self._lock:
            c.execute(
                "UPDATE jobs SET meta_json=?, updated_at=? WHERE id=?",
                (json.dumps(meta), time.time(), jid),
            )

    def cancel(self, jid: str) -> None:
        now = time.time()
        c = self._conn()
        with self._lock:
            c.execute(
                """UPDATE jobs SET status='cancelled', updated_at=?
                   WHERE id=? AND status IN ('pending','running')""",
                (now, jid),
            )

    def is_cancelled(self, jid: str) -> bool:
        row = self.get(jid)
        return row is not None and row["status"] == "cancelled"

    def reap_stale(self, retention_seconds: float) -> int:
        """Delete finished jobs older than `retention_seconds`.

        Only terminal-status jobs are eligible. Returns count deleted."""
        cutoff = time.time() - retention_seconds
        c = self._conn()
        with self._lock:
            cur = c.execute(
                """DELETE FROM jobs
                   WHERE status IN ('done','error','cancelled','interrupted')
                     AND updated_at < ?""",
                (cutoff,)
            )
            return cur.rowcount or 0

    # ---- streaming ----
    def stream(self, jid: str, *, heartbeat: float = 15.0,
               poll_interval: float = 0.5,
               max_idle_seconds: float = 300.0,
               include_rows: bool = True) -> Generator[dict, None, None]:
        """Yield SSE-friendly event dicts as the job progresses.

        Events:
          {"type":"progress", "completed":n, "total":m, "new_cases":[...]}
          {"type":"heartbeat"}
          {"type":"done", "result":[...]}
          {"type":"error", "error":"..."}
          {"type":"cancelled"}
          {"type":"not_found"}

        The loop polls get_progress() (the small columns only), NOT get(), so it
        no longer deserialises the whole 1+ MB partial blob 2-4x/second per
        client. Change is detected on `completed` alone — it bumps once per task,
        which is exactly the cadence we want to emit an event.

        include_rows (default True, legacy-preserving) controls whether progress
        events carry the `new_cases` delta and whether `done` carries `result`.
        The new client passes include_rows=False for progress and reads rows from
        the /rows endpoint. `done` keeps `result` whenever include_rows so the
        current client, which does dedupeCases(ev.result), still renders.
        """
        last_completed = -1
        last_partial_len = 0
        last_change = time.time()
        last_heartbeat = time.time()

        while True:
            prog = self.get_progress(jid)
            if prog is None:
                yield {"type": "not_found"}
                return

            meta = prog.get("meta") or {}
            degraded = bool(meta.get("degraded"))
            status = prog["status"]

            # Detect progress on `completed` (monotonic, one bump per task).
            if prog["completed"] != last_completed:
                evt = {
                    "type": "progress",
                    "completed": prog["completed"],
                    "total": prog["total"],
                    "status": status,
                    "degraded": degraded,
                    "meta": meta,
                }
                if include_rows:
                    rows = self.get_rows(jid)
                    partial = (rows or {}).get("partial_cases") or []
                    evt["new_cases"] = partial[last_partial_len:]
                    last_partial_len = len(partial)
                yield evt
                last_completed = prog["completed"]
                last_change = time.time()

            # Terminal states
            if status == "done":
                done_evt = {"type": "done", "degraded": degraded, "meta": meta}
                # Keep `result` whenever we're shipping rows: the current client
                # does dedupeCases(ev.result) and renders nothing without it.
                if include_rows:
                    done_evt["result"] = (self.get_rows(jid) or {}).get("result")
                yield done_evt
                return
            if status == "error":
                yield {
                    "type": "error",
                    "error": prog["error"],
                    "meta": meta,
                }
                return
            if status == "cancelled":
                yield {"type": "cancelled", "meta": meta}
                return
            if status == "interrupted":
                yield {
                    "type": "error",
                    "error": "job was interrupted (process restart)",
                    "meta": meta,
                }
                return

            now = time.time()
            if now - last_heartbeat >= heartbeat:
                yield {"type": "heartbeat"}
                last_heartbeat = now

            if now - last_change > max_idle_seconds:
                yield {"type": "error", "error": "stream idle timeout"}
                return

            time.sleep(poll_interval)

    # ---- test helpers ----
    def _set_updated_at_for_test(self, jid: str, ts: float) -> None:
        """Override updated_at — used by tests to simulate aged rows."""
        c = self._conn()
        with self._lock:
            c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (ts, jid))
