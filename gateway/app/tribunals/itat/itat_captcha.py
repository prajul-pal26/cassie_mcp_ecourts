"""itat_captcha.py — ITAT case-status captcha solver (audio LEAK + OCR fallback).

The ITAT captcha (https://itat.gov.in/captcha/show) is a hard, mixed-case,
colour-noised 6-char image that plain OCR reads at ~10-30%. Two facts about the
site make a near-100% bypass possible WITHOUT reading the image:

  1. `/captcha/listen/`  — the accessibility endpoint returns an MP3 that SPEAKS
     the 6 characters (one spoken glyph per character, separated by silence).
     This is a LEAK: the audio is a deterministic TTS of the exact answer.
  2. `/Ajax/checkCaptcha` — an ORACLE that says whether a guess is right, for
     free, and it does NOT rotate the captcha (only `/captcha/show` rotates).
     Validation is CASE-INSENSITIVE, so we only need 36 classes (0-9, a-z).

Strategy (in order):
  A. AUDIO DECODE (primary leak): decode the MP3 → split into 6 segments by
     silence → match each segment against a per-character audio template bank
     (itat_captcha_refs.pkl, built by tools/build_captcha_refs.py) → 6 chars.
     Verify the decoded string against the oracle; if valid, done (no image OCR).
  B. OCR + ORACLE RETRY (fallback): ddddocr the image, check via the oracle,
     refresh and retry until valid. Always terminates in a few tries.

Both paths return a captcha string that the oracle has CONFIRMED valid, so the
subsequent form submit never fails on captcha.

Self-contained; depends only on curl_cffi, numpy, ddddocr, and ffmpeg (CLI).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

_BASE = "https://itat.gov.in"
_PAGE = f"{_BASE}/judicial/casestatus"
_SHOW = f"{_BASE}/captcha/show"
_LISTEN = f"{_BASE}/captcha/listen/"
_CHECK = f"{_BASE}/Ajax/checkCaptcha"

_REFS_PATH = Path(__file__).resolve().parent / "itat_captcha_refs.pkl"

_ocr = None
_ocr_lock = __import__("threading").Lock()
_refs = None  # {char: np.ndarray template} loaded lazily


def _get_ocr():
    global _ocr
    if _ocr is None:
        with _ocr_lock:
            if _ocr is None:
                import ddddocr
                _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


# ---------------------------------------------------------------------------
# Audio DSP — decode MP3, split into character segments, featurise
# ---------------------------------------------------------------------------

SR = 8000
FEAT_LEN = 3000       # samples each segment is resampled to before FFT features


def mp3_to_pcm(mp3: bytes) -> np.ndarray:
    """Decode MP3 bytes → mono 8 kHz float32 PCM via ffmpeg."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-i", "pipe:0",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"],
        input=mp3, capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)


def split_segments(pcm: np.ndarray, thr: float = 0.06) -> List[np.ndarray]:
    """Split spoken audio into character segments on silence (energy envelope)."""
    if pcm.size == 0:
        return []
    a = np.abs(pcm)
    a = a / (a.max() + 1e-9)
    win = 160
    env = np.convolve(a, np.ones(win) / win, "same")
    voiced = env > thr
    out: List[np.ndarray] = []
    i, n = 0, len(voiced)
    while i < n:
        if voiced[i]:
            j = i
            # bridge short gaps (<0.1s) inside one spoken glyph
            while j < n and (voiced[j] or (j + 800 < n and voiced[j:j + 800].any())):
                j += 1
            if j - i > 400:
                out.append(pcm[i:j])
            i = j
        else:
            i += 1
    return out


def segment_feature(seg: np.ndarray) -> np.ndarray:
    """Fixed-length spectral fingerprint of one character segment
    (mean+std of log-magnitude STFT frames). Robust to small length jitter."""
    x = np.interp(np.linspace(0, len(seg) - 1, FEAT_LEN),
                  np.arange(len(seg)), seg)
    x = x / (np.abs(x).max() + 1e-9)
    frames = []
    for st in range(0, FEAT_LEN - 256, 128):
        w = x[st:st + 256] * np.hanning(256)
        frames.append(np.log(np.abs(np.fft.rfft(w)) + 1e-6))
    F = np.array(frames)
    return np.concatenate([F.mean(0), F.std(0)])


def _load_refs():
    global _refs
    if _refs is None:
        import pickle
        if _REFS_PATH.exists():
            _refs = pickle.load(open(_REFS_PATH, "rb"))
        else:
            _refs = {}
    return _refs


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def decode_audio(mp3: bytes) -> Optional[str]:
    """Decode the captcha MP3 to a 6-char string using the template bank.
    Returns None if the audio doesn't split into 6 segments or no refs loaded."""
    refs = _load_refs()
    if not refs:
        return None
    segs = split_segments(mp3_to_pcm(mp3))
    if len(segs) != 6:
        return None
    chars = list(refs.keys())
    mats = {c: refs[c] for c in chars}
    out = []
    for seg in segs:
        f = segment_feature(seg)
        best, bestc = -2.0, None
        for c in chars:
            score = _cos(f, mats[c])
            if score > best:
                best, bestc = score, c
        out.append(bestc or "?")
    return "".join(out)


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

def _headers(csrf: str) -> dict:
    return {"Referer": _PAGE, "X-CSRF-TOKEN": csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded"}


def check_captcha(session, csrf: str, value: str) -> bool:
    """Ask the oracle whether `value` is the current captcha (case-insensitive,
    non-rotating)."""
    try:
        r = session.post(_CHECK, data={"captcha": value},
                         headers=_headers(csrf), timeout=30)
        return '"true"' in r.text
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public: obtain a captcha value the oracle has CONFIRMED valid
# ---------------------------------------------------------------------------

def solve(session, csrf: str, *, max_rounds: int = 45) -> Optional[str]:
    """Return a captcha string confirmed valid for THIS session's current
    captcha. Tries the audio leak first (cheap, deterministic), then OCR+oracle
    retry. The returned value stays valid for the immediate form submit because
    /Ajax/checkCaptcha does not rotate the captcha.

    IMPORTANT: do NOT call /captcha/show between solve() and the form submit —
    that would rotate the captcha and invalidate the returned value.
    """
    ref_ready = bool(_load_refs())
    for _ in range(max_rounds):
        # fetch the current captcha image (rotates to a fresh captcha)
        try:
            img = session.get(_SHOW, headers={"Referer": _PAGE}, timeout=30).content
        except Exception:
            continue
        if img[:4] != b"\x89PNG":
            continue

        # A) audio leak
        if ref_ready:
            try:
                audio = session.get(_LISTEN, headers={"Referer": _PAGE}, timeout=30).content
                dec = decode_audio(audio)
                if dec and len(dec) == 6 and check_captcha(session, csrf, dec):
                    return dec
            except Exception:
                pass

        # B) OCR + oracle
        try:
            guess = "".join(c for c in _get_ocr().classification(img) if c.isalnum()).lower()
            if len(guess) == 6 and check_captcha(session, csrf, guess):
                return guess
        except Exception:
            pass
    return None
