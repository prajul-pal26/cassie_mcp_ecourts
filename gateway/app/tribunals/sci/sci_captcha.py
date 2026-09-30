"""SCI captcha solver — the shared "captcha service" every SCI search uses.

What the captcha is
-------------------
sci.gov.in protects each search form with the **securimage-wp (SIWP)** plugin.
The challenge is a small **math-equation image** (e.g. `7 + 5`, `9 - 4`, `3 x 2`)
served at  GET /?_siwp_captcha&id=<scid> , with the same equation also available
as spoken audio at  GET /?_siwp_play&id=<scid> .

Why we solve the equation (no token / no-OCR path exists)
--------------------------------------------------------
We checked the "best" bypasses first:
  * The answer is **not** leaked in the page (unlike SAT) — it lives server-side.
  * The server **does** validate it: a wrong value → `{"message":"The captcha
    code entered was incorrect."}`. So it cannot simply be skipped.
So the only route is to *solve the equation*. The image reads far more reliably
than the audio (audio would need heavyweight ASR), so we OCR the equation image
with ddddocr, evaluate it, and — because a fresh equation costs one cheap GET —
**retry-until-valid** on the rare operator mis-read. That is the same
"retry-until-valid ⇒ ~100%" pattern the Andhra HC scraper uses. The audio URL is
still surfaced by the demo endpoint for transparency / manual fallback.

Public surface
--------------
  solve(session, scid) -> (answer:int, equation:str) | (None, raw_ocr)
      Solve the captcha bound to `scid` on an existing session.
  demo(proxies=None)   -> dict
      Fetch a page, solve one captcha, and return the scid / equation / answer /
      attempts (+ audio URL) so the `/sci/captcha` endpoint can prove it works.
"""
from __future__ import annotations

import re
import threading

try:                                    # browser-impersonated HTTP (preferred)
    from curl_cffi import requests as _rq
    _IMP = "chrome"
except Exception:                       # pragma: no cover
    import requests as _rq
    _IMP = None

BASE = "https://www.sci.gov.in"
_CAPTCHA_IMG = BASE + "/?_siwp_captcha&id={scid}"
_CAPTCHA_AUD = BASE + "/?_siwp_play&id={scid}"

# ddddocr is heavy to construct (one shared instance) and its inference isn't
# guaranteed thread-safe; a lock serialises the (~tens-of-ms) classify call so
# many concurrent users share it safely. Network I/O stays fully parallel.
_OCR = None
_OCR_LOCK = threading.Lock()


def _classify(img: bytes) -> str:
    global _OCR
    with _OCR_LOCK:
        if _OCR is None:
            import ddddocr
            _OCR = ddddocr.DdddOcr(show_ad=False)
        return _OCR.classification(img)


# ddddocr occasionally emits a look-alike glyph for a digit; normalise before
# parsing the equation.
_DIGIT_FIX = str.maketrans({"l": "1", "I": "1", "|": "1", "O": "0", "o": "0",
                            "S": "5", "s": "5", "B": "8", "Z": "2", "g": "9"})


def _parse_equation(raw: str):
    """`'7+5'`/`'9 - 4'`/`'3x2'` → (answer, 'a op b = answer'); None on failure."""
    s = (raw or "").translate(_DIGIT_FIX)
    s = s.replace("×", "x").replace("X", "x").replace("*", "x").replace("÷", "/")
    m = re.search(r"(\d+)\s*([+\-x/])\s*(\d+)", s)
    if not m:
        return None
    a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
    ans = {"+": a + b, "-": a - b, "x": a * b,
           "/": (a // b if b else None)}[op]
    if ans is None:
        return None
    return ans, f"{a} {op} {b} = {ans}"


def solve(session, scid: str):
    """OCR + evaluate the equation image bound to `scid`. Returns
    (answer:int, equation:str) or (None, raw_ocr_text) if unparsable."""
    img = session.get(_CAPTCHA_IMG.format(scid=scid), timeout=30).content
    raw = _classify(img)
    parsed = _parse_equation(raw)
    if not parsed:
        return None, raw
    return parsed[0], parsed[1]


def _session(proxies=None):
    s = _rq.Session(impersonate=_IMP) if _IMP else _rq.Session()
    if proxies:
        s.proxies = proxies
    return s


def demo(proxies=None) -> dict:
    """Solve one live captcha and report it — powers GET /sci/captcha so you can
    confirm the solver works. Uses the diary-no page (cheapest form)."""
    from app.tribunals.sci import fetch_sci as sci
    for attempt in range(1, 9):
        s = _session(proxies)
        html = s.get(f"{BASE}/case-status-diary-no/", timeout=30).text
        fields = sci._harvest(html, "/case-status-diary-no/")
        scid = fields.get("scid")
        if not scid:
            continue
        ans, eq = solve(s, scid)
        if ans is None:
            continue
        return {
            "success": True,
            "scid": scid,
            "equation": eq,
            "answer": ans,
            "attempts": attempt,
            "captcha_image_url": _CAPTCHA_IMG.format(scid=scid),
            "captcha_audio_url": _CAPTCHA_AUD.format(scid=scid),
            "note": "Solved by OCR-ing the equation image and evaluating it; "
                    "every SCI search reuses this solver with retry-until-valid.",
        }
    return {"success": False, "error": "captcha OCR failed after 8 attempts"}
