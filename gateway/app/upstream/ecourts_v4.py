"""
EcourtFetch_v4 — client for the v4.0 eCourts mobile API.

  Base URLs:
    DC: https://app.ecourts.gov.in/services_DC_4.0/
    HC: https://app.ecourts.gov.in/services_HC_4.0/

  Reverse-engineered from the React-Native eCourts mobile app
  (in.gov.ecourts.eCourtsServices), via Hermes bytecode disassembly
  + live mitmproxy capture.

──────────────────────────────────────────────────────────────────────────
  Crypto envelope (same scheme for both DC and HC)
──────────────────────────────────────────────────────────────────────────

  REQUEST:
    GET <base>/<endpoint>?params=<envelope>
       OR  (for display_pdf_new.php on DC)
    POST <base>/display_pdf_new.php
         Content-Type: application/json
         body = {"params": {"params": "<envelope>"}}

    where envelope = randomHex16(16 chars)
                   + globalIndex(1 char, "0"-"5")
                   + base64( AES_128_CBC_PKCS7(JSON(payload),
                                               KEY="MbQeThWmZq4t6w9z",
                                               IV=HEX(globaliv[globalIndex]+randomHex16) ))

  RESPONSE:
    body = "\\r\\n" + responseIvHex(32 chars)
                   + base64( AES_128_CBC_PKCS7(JSON(result),
                                               KEY="2s5v8x/A?D(G+KbP",
                                               IV=HEX(responseIvHex) ))

──────────────────────────────────────────────────────────────────────────
  Field-name differences DC ↔ HC
──────────────────────────────────────────────────────────────────────────
  The same logical field has a different name in DC vs HC payloads:

    "establishment code"  →  DC: court_code_arr   /   HC: court_code
    "district / bench"    →  DC: dist_code        /   HC: dist_code (bench_id)
    causelist scope       →  DC: court_no + flag (civ_t|cri_t)
                             HC: bench_id (from causeListBenchWebService.php)
    FIR search            →  DC: yes              /   HC: not available
    pendingDisposed       →  optional on most     /
                             HC's case-number + filing-number searches
                                  do NOT accept it
    display_pdf_new       →  DC works  /  HC currently returns 302→errormsg

──────────────────────────────────────────────────────────────────────────
  Auth flow
──────────────────────────────────────────────────────────────────────────
  Bootstrap (3 calls, no Authorization header):
    1) getAllLabelsWebService.php
    2) stateWebService.php  (also: action_code:"benches" exists for HC)
    3) appReleaseWebService.php

  After bootstrap the server has set the JSESSION cookie. The next call
  must include  Authorization: Bearer <JWT>  — but the very first JWT is
  embedded in the decrypted JSON response of one of those bootstrap
  calls (the `token` field). Every subsequent response carries a fresh
  `token` field which replaces the bearer for the next call.

──────────────────────────────────────────────────────────────────────────
"""
import base64
import binascii
import json
import logging
import os
import random
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential,
)

from app.core import config
from app.reliability.gateway import (
    UpstreamConnectionError,
    UpstreamHTTPError,
    WafBlocked,
)

log = logging.getLogger("ecourts_v4")


class _CallBudget:
    """Wall-clock budget shared across a single logical _call() invocation
    AND any bootstrap() re-entry it triggers on a WAF hit, so nested
    WAF-cooldown cycles share one ceiling instead of each getting a fresh
    unbounded timer.

    Root-cause fix for a real incident: the old code slept 60s on a WAF-405
    then called bootstrap(), which itself makes 3 more _call() invocations
    — each of which could hit WAF-405 again and recurse into bootstrap()
    again, with no depth limit and no total-time budget. A single stuck
    task (e.g. a single-bench HC search, which has exactly one task) could
    silently block well past the job-stream's 300s idle timeout, producing
    a misleading "stream idle timeout" while the worker was still blocked.
    See config.CALL_TOTAL_BUDGET_SECONDS for the default (90s).
    """

    def __init__(self, max_seconds=None, max_waf_reentries=None):
        if max_seconds is None:
            max_seconds = config.CALL_TOTAL_BUDGET_SECONDS
        if max_waf_reentries is None:
            max_waf_reentries = config.WAF_MAX_REENTRIES
        self.deadline = time.monotonic() + max_seconds
        self.max_waf_reentries = max_waf_reentries
        self.waf_reentries = 0

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def try_enter_waf_reentry(self) -> bool:
        """Structural, wall-clock-INDEPENDENT cap on how many times a
        WAF-405 branch may trigger a bootstrap() re-entry while sharing
        this budget. Returns False once the cap is hit regardless of how
        much time-based budget remains.

        This exists as defense in depth alongside the time-based budget:
        the time budget alone only bounds recursion because a real
        time.sleep(60) consumes real wall-clock time between cycles. That
        holds in production, but correctness for a "zero possibility of
        breakage" bar shouldn't rest solely on an environmental assumption
        about sleep() actually blocking — this counter bounds total
        bootstrap re-entries to a small constant unconditionally.
        """
        if self.waf_reentries >= self.max_waf_reentries:
            return False
        self.waf_reentries += 1
        return True


class _TransientHTTPStatus(Exception):
    """Internal signal: a 5xx response that tenacity should retry. Never
    escapes _call() — always converted to UpstreamHTTPError if retries are
    exhausted."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"transient HTTP {status_code}")


# Bounded retry for connection errors / transient 5xx on a SINGLE attempt's
# transport call. `stop_after_attempt(3) | stop_after_delay(45)` — whichever
# bound is hit first stops retrying; never attempt-count alone (a fast
# succession of near-instant failures could otherwise retry indefinitely
# within a single "attempt count" budget). Only genuinely transient
# transport-layer signals retry here — a 4xx or a decode failure is never
# retried by tenacity.
_transport_retry = retry(
    stop=(stop_after_attempt(3) | stop_after_delay(45)),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((
        requests.exceptions.ConnectionError,
        requests.exceptions.ReadTimeout,
        _TransientHTTPStatus,
    )),
    reraise=True,
)


# ── Crypto constants (from Hermes bytecode disassembly) ────────────────
REQUEST_KEY = binascii.unhexlify("4D6251655468576D5A7134743677397A")   # "MbQeThWmZq4t6w9z"
RESPONSE_KEY = binascii.unhexlify("3273357638782F413F4428472B4B6250")  # "2s5v8x/A?D(G+KbP"

# Per-session-rotating IV halves, looked up by globalIndex (0..5).
# Generated client-side by generateGlobalIv() (Function #16198 in the JS bundle).
GLOBALIV_TABLE = [
    "556A586E32723575",  # UjXn2r5u
    "34743777217A2543",  # 4t7w!z%C
    "413F4428472B4B62",  # A?D(G+Kb
    "48404D635166546A",  # H@McQfTj
    "614E645267556B58",  # aNdRgUkX
    "655368566D597133",  # eShVmYq3
]

UID = "in.gov.ecourts.eCourtsServices"

DC_BASE = "https://app.ecourts.gov.in/services_DC_4.0/"
HC_BASE = "https://app.ecourts.gov.in/services_HC_4.0/"

DEFAULT_HEADERS = {
    "User-Agent": "okhttp/4.9.2",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip",
}


# ── Crypto helpers ─────────────────────────────────────────────────────

def _gen_random_hex16():
    """8 random bytes → 16 hex chars."""
    return binascii.hexlify(os.urandom(8)).decode("ascii")


def encrypt_request_payload(payload, global_index=None):
    """Return the value for `?params=` / inner `params` field.

    payload (dict) → randomHex16 + str(globalIndex) + base64(ciphertext)
    """
    if global_index is None:
        global_index = random.randint(0, len(GLOBALIV_TABLE) - 1)
    random_hex16 = _gen_random_hex16()
    iv = binascii.unhexlify(GLOBALIV_TABLE[global_index] + random_hex16)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ct = AES.new(REQUEST_KEY, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    return random_hex16 + str(global_index) + base64.b64encode(ct).decode("ascii")


def decrypt_response_body(body):
    """Decrypt a v4 response body; return parsed JSON, raw string, or None."""
    if body is None:
        return None
    s = body.strip()
    if len(s) < 33:
        # Sometimes upstream returns plain unencrypted JSON (e.g. errormsg.php
        # returns {"status":"N","msg":"error"} as a redirect target).
        try:
            return json.loads(s)
        except Exception:
            return None
    # Try AES-decrypt first
    try:
        iv = binascii.unhexlify(s[:32])
        ct = base64.b64decode(s[32:])
        pt = unpad(AES.new(RESPONSE_KEY, AES.MODE_CBC, iv).decrypt(ct), AES.block_size)
        text = pt.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    except Exception:
        # Fallback: maybe it's plain JSON (e.g. errormsg.php after a 302)
        try:
            return json.loads(s)
        except Exception:
            return None


# ── Client ──────────────────────────────────────────────────────────────

class EcourtFetchV4:
    """Stateful client for the v4.0 eCourts API.

    One instance per court_type. Maintains JSESSION + rotating JWT.
    Use the public methods listed below; all of them encrypt payloads,
    rotate auth, and parse responses.
    """

    def __init__(self, court_type="dc", proxies=None, timeout=30):
        self.court_type = court_type
        self.is_hc = (court_type == "hc")
        self.base = HC_BASE if self.is_hc else DC_BASE
        # Field-name used for the "establishment code" — DC: court_code_arr, HC: court_code
        self.court_field = "court_code" if self.is_hc else "court_code_arr"

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if proxies:
            self.session.proxies.update(proxies)
        self.session.verify = False
        self.timeout = timeout
        self._token = None
        self.global_index = random.randint(0, 5)

    # ── transport ──────────────────────────────────────────────────────
    @_transport_retry
    def _encrypt_and_send(self, url, body, method):
        """One attempt: fresh IV each call (tenacity re-invokes this whole
        method on retry, so re-encryption happens naturally per attempt,
        matching the original "re-encrypt each retry" behavior).

        Raises _TransientHTTPStatus on a 5xx so tenacity retries it;
        requests.exceptions.ConnectionError/ReadTimeout propagate natively
        for tenacity to catch. Both are internal-only — _call() converts
        whatever survives tenacity's retry budget into a typed
        UpstreamConnectionError/UpstreamHTTPError before it can reach the
        breaker, so the breaker only ever sees genuine upstream-health
        signals (see reliability/gateway.py's UpstreamFailure taxonomy).
        """
        env = encrypt_request_payload(body, self.global_index)
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if method == "POST":
            headers["Content-Type"] = "application/json"
            r = self.session.post(
                url, data=json.dumps({"params": {"params": env}}),
                headers=headers, timeout=self.timeout,
                allow_redirects=False)
        else:
            r = self.session.get(
                url, params={"params": env}, headers=headers,
                timeout=self.timeout, allow_redirects=False)
        if r.status_code in (500, 502, 503, 504):
            raise _TransientHTTPStatus(r.status_code)
        return r

    def _call(self, endpoint, payload, *, require_auth=True, method="GET",
              max_retries=3, budget=None):
        """Encrypt payload, send, decrypt response, rotate token if present.

        Retries on transient connection errors / 5xx with exponential
        backoff (eCourts upstream regularly drops idle connections / 502s
        under load) via the module-level `_transport_retry` decorator on
        `_encrypt_and_send` — bounded by BOTH attempt count and wall-clock
        delay, never attempt-count alone.

        `budget` is a `_CallBudget` bounding the TOTAL wall-clock time this
        call (including any bootstrap() re-entry it triggers on a WAF hit)
        is allowed to take. If not supplied, a fresh one is created — every
        top-level call gets its own ceiling, not one shared across
        unrelated requests. `max_retries` is kept as a parameter for
        backward-compatible call sites but no longer drives the transport
        retry loop directly (see _encrypt_and_send) — it's unused here and
        retained only so existing call sites don't need updating.
        """
        if budget is None:
            budget = _CallBudget()
        if require_auth and self._token is None:
            self.bootstrap(budget=budget)
        url = self.base + endpoint
        body = dict(payload)
        body.setdefault("uid", UID)
        body.setdefault("bilingual_flag", 0)

        # [ECOURTS_DBG] — log plaintext payload before AES encryption.
        # Gated by env var so production stays quiet by default.
        if os.environ.get("ECOURTS_DEBUG_LOG") == "1":
            try:
                log.warning(
                    "[ECOURTS_DBG] REQUEST endpoint=%s court_type=%s payload=%s",
                    endpoint, self.court_type,
                    json.dumps(body, separators=(",", ":"), default=str),
                )
            except Exception:
                pass

        # Outer loop handles WAF-405 cooldown cycles specifically — bounded
        # both by a hard cycle cap (defense in depth) AND by the shared
        # `budget`, which is what actually fixes the original unbounded-
        # recursion bug (a WAF hit inside bootstrap() shares this SAME
        # budget, so nested cooldowns can't each reset the clock).
        max_waf_cycles = 3
        for _waf_cycle in range(max_waf_cycles):
            if budget.expired():
                raise WafBlocked(
                    f"{endpoint}: call budget exhausted before completion "
                    f"(CALL_TOTAL_BUDGET_SECONDS={config.CALL_TOTAL_BUDGET_SECONDS})")

            try:
                r = self._encrypt_and_send(url, body, method)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout) as e:
                raise UpstreamConnectionError(str(e)) from e
            except _TransientHTTPStatus as e:
                raise UpstreamHTTPError(
                    e.status_code,
                    f"{endpoint}: HTTP {e.status_code} after retries") from e

            if r.status_code == 302:
                return {"status": "N", "msg": "redirected to errormsg",
                        "redirect_to": r.headers.get("Location")}

            # 405 + HTML body == Sucuri WAF block. Cooldown + force re-bootstrap.
            if r.status_code == 405 and "_event_transid" in r.text:
                remaining = budget.remaining()
                if remaining < 60:
                    # Not enough budget left to sleep 60s and still do
                    # useful work — surface immediately instead of sleeping
                    # past our own deadline. The breaker's own
                    # waf_reset_timeout (~65 min, see reliability/gateway.py)
                    # is the real backoff mechanism from here.
                    raise WafBlocked(
                        f"{endpoint}: WAF block, insufficient budget "
                        f"remaining ({remaining:.1f}s) to retry")
                if not budget.try_enter_waf_reentry():
                    # Structural cap hit — see _CallBudget.try_enter_waf_
                    # reentry's docstring. Bounds recursion depth even if
                    # something in the environment ever made time.sleep()
                    # not actually consume wall-clock time (tests included).
                    raise WafBlocked(
                        f"{endpoint}: WAF block, exhausted "
                        f"{budget.max_waf_reentries} bootstrap re-entries "
                        "for this call budget")
                log.warning(
                    "WAF block on %s — sleeping 60s + re-bootstrap "
                    "(budget remaining: %.1fs, reentry %d/%d)",
                    endpoint, remaining, budget.waf_reentries,
                    budget.max_waf_reentries)
                time.sleep(60)
                self._token = None
                try:
                    self.bootstrap(budget=budget)
                except Exception:
                    pass
                continue

            if r.status_code != 200:
                raise UpstreamHTTPError(
                    r.status_code,
                    f"{endpoint}: HTTP {r.status_code}, body={r.text[:200]!r}")

            result = decrypt_response_body(r.text)

            # [ECOURTS_DBG] — log decrypted response shape so we can see
            # exactly what the upstream is handing back. Truncated to 4 KB
            # to keep log lines manageable.
            if os.environ.get("ECOURTS_DEBUG_LOG") == "1":
                try:
                    if isinstance(result, dict):
                        # Summarise the response: top-level keys, status/msg if
                        # present, count of numeric-key buckets, sample case.
                        keys = list(result.keys())
                        status = result.get("status")
                        msg = result.get("msg")
                        num_buckets = sum(
                            1 for k in keys if isinstance(k, str) and k.isdigit()
                        )
                        sample = None
                        for k in keys:
                            if isinstance(k, str) and k.isdigit():
                                v = result[k]
                                if isinstance(v, dict):
                                    case_nos = v.get("caseNos") or []
                                    sample = {
                                        "court_code": v.get("court_code"),
                                        "establishment_name": v.get("establishment_name"),
                                        "caseNos_len": len(case_nos),
                                        "first_case": case_nos[0] if case_nos else None,
                                    }
                                    break
                        log.warning(
                            "[ECOURTS_DBG] RESPONSE endpoint=%s status=%r msg=%r "
                            "top_keys=%s num_buckets=%d sample=%s",
                            endpoint, status, msg,
                            json.dumps(keys, default=str)[:200],
                            num_buckets,
                            json.dumps(sample, separators=(",", ":"), default=str)[:1500]
                            if sample else None,
                        )
                    else:
                        log.warning(
                            "[ECOURTS_DBG] RESPONSE endpoint=%s non-dict-type=%s value=%r",
                            endpoint, type(result).__name__, str(result)[:500],
                        )
                except Exception as _dbg_e:
                    log.warning("[ECOURTS_DBG] response-log error: %r", _dbg_e)

            if isinstance(result, dict) and result.get("token"):
                self._token = result["token"]
            return result

        # Exhausted the WAF-cycle cap without success or a clean WafBlocked
        # raise above (shouldn't normally be reached — the budget check at
        # the top of the loop should trip first — but kept as a hard
        # backstop against any future change to the loop body).
        raise WafBlocked(
            f"{endpoint}: exhausted {max_waf_cycles} WAF-cooldown cycles "
            "without success")

    # ── bootstrap (no Bearer needed) ───────────────────────────────────
    def bootstrap(self, budget=None):
        """Bootstrap session with eCourts v4.

        Order matters as of 2026: eCourts now returns
        `{"status": "N", "Msg": "Not in session ! auth_token = "}` for
        getAllLabels and stateWebService if no auth_token is set, but
        appReleaseWebService.php (the only token-issuing endpoint) works
        unauthenticated. So we must call appRelease FIRST to obtain the
        token, then the other two endpoints succeed and complete the
        session handshake. Without this order, the session is left
        half-validated and search endpoints return HTTP 500.

        `budget` is threaded through to all 3 internal _call() invocations
        so that if THIS bootstrap was itself triggered by a WAF-405 inside
        another _call() (see above), any WAF hit during bootstrap shares
        that SAME wall-clock ceiling instead of getting its own fresh,
        unbounded one — this is the fix for the original recursion bug.
        If bootstrap() is called directly (not via a WAF re-entry), a
        fresh budget is created as usual.
        """
        if budget is None:
            budget = _CallBudget()
        # 1. appRelease FIRST — this issues the auth token.
        try:
            self._call("appReleaseWebService.php",
                       {"version": "4.0"}, require_auth=False, budget=budget)
        except Exception as e:
            log.warning("bootstrap appReleaseWebService failed: %s", e)
        # 2. Now that self._token is set, the next two calls carry the
        #    Authorization header automatically (see _call()).
        try:
            self._call("getAllLabelsWebService.php",
                       {"language_flag": "english"}, require_auth=False,
                       budget=budget)
        except Exception as e:
            log.warning("bootstrap getAllLabels failed: %s", e)
        try:
            self._call("stateWebService.php",
                       {"action_code": 1}, require_auth=False, budget=budget)
        except Exception as e:
            log.warning("bootstrap stateWebService failed: %s", e)
        return self._token is not None

    # ── Metadata ───────────────────────────────────────────────────────
    def list_states(self):
        # action_code=1 = list states. For HC, "benches" returns benches per state.
        return self._call("stateWebService.php", {"action_code": 1})

    def list_districts(self, state_code):
        # For HC, action_code="benches" gets the bench list.
        body = {"state_code": state_code}
        if self.is_hc:
            body["action_code"] = "benches"
        return self._call("districtWebService.php", body)

    def list_court_complexes(self, state_code, dist_code):
        """DC only — HC doesn't have complexes."""
        if self.is_hc:
            raise NotImplementedError("court complexes are DC-only")
        return self._call("courtEstWebService.php", {
            "state_code": state_code, "dist_code": dist_code,
            "action_code": "fillCourtComplex"})

    def list_case_types(self, state_code, dist_code, court_code):
        return self._call("caseTypesWebService.php", {
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "language_flag": "english"})

    def list_acts(self, state_code, dist_code, court_code, search_text=None):
        body = {"state_code": state_code, "dist_code": dist_code,
                self.court_field: court_code}
        if not self.is_hc:
            body["language_flag"] = "english"
        if search_text:
            body["searchText"] = search_text
        return self._call("actWebService.php", body)

    def list_police_stations(self, state_code, dist_code, court_code):
        """DC only — HC has no police stations indexed."""
        if self.is_hc:
            raise NotImplementedError("police stations are DC-only")
        return self._call("policeStationWebService.php", {
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "language_flag": "english"})

    def list_court_names(self, state_code, dist_code, court_code):
        """DC only — list of judges/court-rooms per establishment."""
        if self.is_hc:
            raise NotImplementedError("court_names are DC-only")
        return self._call("courtNameWebService.php", {
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "language_flag": "english"})

    # ── Case detail ────────────────────────────────────────────────────
    def case_history(self, cino):
        return self._call("caseHistoryWebService.php",
                          {"cino": cino, "language_flag": "english"})

    # ── Search endpoints ───────────────────────────────────────────────
    def search_by_act(self, state_code, dist_code, court_code, *,
                      act_type, pendingDisposed="Pending"):
        """Search cases by Act. The act code goes in `selectActTypeText` — that
        is the ONLY act field the upstream reads; act_name / under_section are
        NOT required (sending them, or a numeric value with the wrong status,
        makes the endpoint 500). `pendingDisposed` MUST be a single 'Pending' or
        'Disposed' — 'Both' is rejected (the bridge loops the two)."""
        return self._call("searchByActWebService.php", {
            "selectActTypeText": str(act_type),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "pendingDisposed": pendingDisposed})

    def search_by_party_name(self, state_code, dist_code, court_code, *,
                             pet_name, year="", pendingDisposed="Both"):
        return self._call("searchByPartyName.php", {
            "pet_name": pet_name, "year": str(year),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "pendingDisposed": pendingDisposed})

    def search_by_fir(self, state_code, dist_code, court_code, *,
                      police_stationcode, uniform_code,
                      firNumber, year, pendingDisposed="Both"):
        """DC only — HC has no FIR search."""
        if self.is_hc:
            raise NotImplementedError("FIR search is DC-only")
        return self._call("firNumberSearch.php", {
            "police_stationcode": str(police_stationcode),
            "uniform_code": uniform_code,
            "firNumber": str(firNumber), "year": str(year),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "language_flag": "english",
            "pendingDisposed": pendingDisposed})

    def search_by_filing_number(self, state_code, dist_code, court_code, *,
                                filingNumber, year):
        # HC does NOT take pendingDisposed for filing-number; DC payload also
        # doesn't include it in our captures.
        return self._call("searchByFilingNumberWebService.php", {
            "filingNumber": str(filingNumber), "year": str(year),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code})

    def search_by_advocate(self, state_code, dist_code, court_code, *,
                           advocateName, checkedSearchByRadioValue="1",
                           pendingDisposed="Both"):
        """checkedSearchByRadioValue: '1'=by name, '2'=by bar registration."""
        return self._call("searchByAdvocateName.php", {
            "advocateName": advocateName,
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "checkedSearchByRadioValue": str(checkedSearchByRadioValue),
            "pendingDisposed": pendingDisposed})

    def search_by_advocate_barcode(self, state_code, dist_code, court_code, *,
                                   barstatecode, barcode, year, date="",
                                   pendingDisposed="Both"):
        """Advocate search by Bar Registration number (e.g. MP/687/2012).

        checkedSearchByRadioValue="2" with the bar registration split into:
          barstatecode = the STATE STRING (e.g. "MP"), NOT a numeric code
          barcode      = the registration number ("687")
          year         = the registration year ("2012")
          date         = "" (unused for bar search)
        advocateName is sent empty. Verified live against MP/687/2012.
        """
        return self._call("searchByAdvocateName.php", {
            "advocateName": "",
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "checkedSearchByRadioValue": "2",
            "barstatecode": str(barstatecode),
            "barcode": str(barcode),
            "year": str(year),
            "date": str(date),
            "pendingDisposed": pendingDisposed})

    def search_by_case_number(self, state_code, dist_code, court_code, *,
                              case_type, case_number, year):
        # HC observed shape has NO pendingDisposed; same for DC in our captures.
        return self._call("caseNumberSearch.php", {
            "case_type": str(case_type), "case_number": str(case_number),
            "year": str(year),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code})

    def search_by_case_type(self, state_code, dist_code, court_code, *,
                            case_type, year="", pendingDisposed="Both"):
        return self._call("searchByCaseType.php", {
            "case_type": str(case_type), "year": str(year),
            "state_code": state_code, "dist_code": dist_code,
            self.court_field: court_code,
            "pendingDisposed": pendingDisposed})

    # ── Causelist endpoints ────────────────────────────────────────────
    def list_benches_with_causelist(self, state_code, dist_code, court_code, *, date):
        """HC: list judges/benches sitting on a given date.

        DC also exposes this endpoint but it usually returns empty — DC's
        daily list is per court_no (see daily_causelist_dc).
        """
        return self._call("causeListBenchWebService.php", {
            "state_code": state_code, "dist_code": dist_code,
            "court_code": court_code, "date": date})

    def daily_causelist_dc(self, state_code, dist_code, court_code, *,
                           court_no, causelist_date, flag="civ_t",
                           selprevdays=0):
        """DC daily cause list for a specific court_no.

        flag: 'civ_t' (civil) or 'cri_t' (criminal). The mobile app makes
        two calls — one for each — and concatenates results.
        """
        if self.is_hc:
            raise NotImplementedError("Use daily_causelist_hc for HC")
        return self._call("cases_new.php", {
            "state_code": state_code, "dist_code": dist_code,
            "court_code": str(court_code), "court_no": str(court_no),
            "flag": flag, "selprevdays": selprevdays,
            "causelist_date": causelist_date, "language_flag": "english"})

    def daily_causelist_hc(self, state_code, dist_code, court_code, *,
                           bench_id, causelist_date, selprevdays=0):
        """HC daily cause list for a specific bench (from
        list_benches_with_causelist)."""
        if not self.is_hc:
            raise NotImplementedError("Use daily_causelist_dc for DC")
        return self._call("cases_new.php", {
            "state_code": state_code, "dist_code": dist_code,
            "court_code": str(court_code), "bench_id": str(bench_id),
            "selprevdays": selprevdays, "causelist_date": causelist_date})

    # ── PDF (judgments / orders) ───────────────────────────────────────
    def display_pdf(self, *, state_cd, dist_cd, court_code, caseno, filename,
                    appFlag="1", cCode=1):
        """Resolve a PDF filename to a download URL.

        DC: returns {status:"Y", pdf_url, token}. The pdf_url is then GET-ed
        (no auth) to fetch the PDF binary.

        ⚠ HC: server is currently broken — returns HTTP 302 → errormsg.php
        for every request. This method will detect the redirect and return
        {"status":"N","msg":"redirected to errormsg",…}.
        """
        return self._call("display_pdf_new.php", {
            "state_cd": state_cd, "dist_cd": dist_cd,
            "court_code": court_code,
            "appFlag": str(appFlag), "caseno": caseno,
            "cCode": cCode, "filename": filename,
            "bilingual_flag": "0"}, method="POST")

    def fetch_pdf_bytes(self, pdf_url):
        """Download the actual PDF bytes from the URL returned by display_pdf.

        The URL already carries its own token; no Bearer needed.
        """
        r = self.session.get(pdf_url, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"pdf fetch HTTP {r.status_code}")
        return r.content


# ── CLI demo ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO)

    print("=== DC bootstrap + states + districts ===")
    dc = EcourtFetchV4(court_type="dc")
    dc.bootstrap()
    states = dc.list_states()
    if isinstance(states, dict) and states.get("states"):
        print(f"  DC states: {len(states['states'])}")
    d = dc.list_districts(state_code=26)
    if isinstance(d, dict) and d.get("districts"):
        print(f"  Delhi DC districts: {len(d['districts'])}")

    print()
    print("=== HC bootstrap + state list ===")
    hc = EcourtFetchV4(court_type="hc")
    hc.bootstrap()
    states = hc.list_states()
    if isinstance(states, dict) and states.get("states"):
        print(f"  HC states: {len(states['states'])}")
        # Find Allahabad (state 13)
        b = hc.list_districts(state_code=13)
        if isinstance(b, dict) and b.get("districts"):
            print(f"  Allahabad HC benches: {len(b['districts'])}")
            for bench in b["districts"][:3]:
                print(f"    {bench['dist_code']}  {bench['dist_name']}")
