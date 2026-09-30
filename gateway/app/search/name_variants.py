"""Spelling-variant generator + fuzzy scoring used by Find My Case search.

Lifted from app.py so app_v4.py can use it without modifying app.py.
Both files keep their own copy of this logic (we don't import from app.py
because app.py is a Flask application, not a pure module).
"""
import difflib
import re


# ── Fuzzy scoring ──────────────────────────────────────────────────────

def fuzz(a, b):
    """Token-aware fuzzy score 0-100.

    For each token of `a`, take the BEST per-token ratio against any token of
    `b` (and also vs full `b`). Average across `a`'s tokens. This handles
    short queries like "Anil" matching against "Aneel Kumar Singh" cleanly.
    """
    if not a or not b:
        return 0
    a = a.lower().strip(); b = b.lower().strip()
    if a == b:
        return 100
    a_tokens = [t for t in re.split(r"[^a-z0-9]+", a) if t]
    b_tokens = [t for t in re.split(r"[^a-z0-9]+", b) if t]
    if not a_tokens or not b_tokens:
        return int(difflib.SequenceMatcher(None, a, b).ratio() * 100)
    scores = []
    for at in a_tokens:
        best = max(
            difflib.SequenceMatcher(None, at, bt).ratio()
            for bt in b_tokens
        )
        full = difflib.SequenceMatcher(None, at, b).ratio()
        scores.append(max(best, full))
    return int(sum(scores) / len(scores) * 100)


# ── Spelling-variant generator for Indian-name transliteration ─────────
# Designed to catch real transliteration drift (Akash↔Aakash, Agarwal↔Aggarwal,
# Kamlesh↔Kamalesh, Anil↔Aneel) without generating nonsense variants
# (akasah, sinagh, singah) that the naive "insert vowel between consonants"
# approach produces.

_VOWELS = set("aeiou")
_DOUBLE_CONS_RE = re.compile(r"([bcdfghjklmnpqrstvwxz])\1+", re.IGNORECASE)
_STOP_CONSONANTS_DOUBLE = set("kgjtdpbmnls")
_H_DIGRAPHS = ["kh", "gh", "bh", "dh", "jh", "ch", "th", "ph"]
_SCHWA_CLUSTERS = {"ml", "mr"}
_BIDI_PAIRS = [
    ("ee", "i"),     # Aneel ↔ Anil
    ("ai", "ay"),    # Vaibhav ↔ Vaybhav
    ("v",  "w"),     # Vinay ↔ Winay (word-initial only — see below)
    ("z",  "j"),     # Zaffar ↔ Jaffar (word-initial only)
    ("ksh", "x"),    # Lakshman ↔ Laxman — MEASURED essential: "Lakshmi"→"Laxmi"
                     # found 60 of 70 rows (primary_variant attribution).
    # Attested spellings the old list missed. "Mohammed"→"Mohammad" returned
    # 211 rows with ZERO overlap (a disjoint record set); "Chaudhary" family
    # is one of the most inconsistent surnames in the index.
    ("mmed", "mmad"),   # Mohammed ↔ Mohammad
    ("audh", "oudh"),   # Chaudhary ↔ Choudhary
    ("audh", "owdh"),   # Chaudhary ↔ Chowdhury (with the ary↔ury below)
    ("ary", "ury"),     # Chaudhary ↔ Chaudhury
]
_SHORT_I_CONTEXT = re.compile(r"i(?:ng|nh|nk|nd|nt|nc)")
_COLLAPSE_ONLY = [
    ("aa", "a"),     # Aanand → Anand
    ("oo", "u"),     # Soonil → Sunil
    ("ph", "f"),     # Phool → Fool
    ("ck", "k"),     # Vickram → Vikram
]
_INITIAL_ONLY = {"v", "w", "z", "j"}


def _word_variants(word, cap=5):
    if not word:
        return []
    lower = word.strip().lower()
    if len(lower) < 2:
        return [lower]
    out = [lower]
    seen = {lower}

    def add(v):
        if v and v != lower and v not in seen:
            out.append(v); seen.add(v)
        return len(out) >= cap

    # Rule 1: Initial-vowel doubling / collapsing
    if lower[0] in _VOWELS:
        if len(lower) > 1 and lower[1] not in _VOWELS:
            if add(lower[0] + lower): return out[:cap]
        if len(lower) > 1 and lower[0] == lower[1]:
            if add(lower[1:]): return out[:cap]

    # Rule 2 (REMOVED): stop-consonant doubling between two vowels
    # (Kumar->Kummar, Soni->Sonni). Measured via primary_variant attribution:
    # the doubled form found ZERO rows the base didn't — it is generative
    # morphology, not an observed spelling. eCourts stores "Kumar", never
    # "Kummar". It was pure upstream cost (one call per court per variant) and
    # it inflated multi-word names badly (Ram Kumar Soni -> 54 tasks). The
    # inverse (Rule 3, collapse doubles) is kept because a user who TYPES a
    # doubled form still needs the collapsed spelling searched.

    # Rule 3: Collapse any doubled consonants
    collapsed = _DOUBLE_CONS_RE.sub(r"\1", lower)
    if add(collapsed): return out[:cap]

    # Rule 4: Bidirectional phonetic substitutions
    for a, b in _BIDI_PAIRS:
        for src, dst in ((a, b), (b, a)):
            if src in _INITIAL_ONLY:
                if lower.startswith(src):
                    if add(dst + lower[len(src):]):
                        return out[:cap]
                continue
            if src in lower:
                if src == "i" and _SHORT_I_CONTEXT.search(lower):
                    continue
                if add(lower.replace(src, dst)):
                    return out[:cap]

    # Rule 5: Collapse-only substitutions
    for src, dst in _COLLAPSE_ONLY:
        if src in lower:
            if add(lower.replace(src, dst)):
                return out[:cap]

    # Rule 6: h-digraph drop
    for d in _H_DIGRAPHS:
        if d in lower:
            if add(lower.replace(d, d[0])):
                return out[:cap]

    # Rule 7: Schwa insertion in whitelisted clusters
    for i in range(len(lower) - 1):
        if lower[i:i+2] in _SCHWA_CLUSTERS:
            if add(lower[:i+1] + "a" + lower[i+1:]):
                return out[:cap]

    return out[:cap]


def name_variants(name, cap=8):
    """Generate up to `cap` phonetic spelling variants for a (possibly
    multi-word) name. Per-word generation then cartesian-product, so
    "Anil Agarwal" produces "Anil Aggarwal" without polluting "Anil".

    Case-handling: the underlying `_word_variants` operates in lowercase
    by design (phonetic rules are easier on lowercase). We promote each
    output back to title-case to match how Indian names typically appear
    in eCourts records. Live testing (Playwright + Fly logs, 2026-05-24)
    confirmed eCourts v4 is case-insensitive on `pet_name`: a search for
    "Arun" in Kanpur Nagar returned 143 cases. So emitting UPPERCASE
    and lowercase variants alongside title-case was 3× wasted upstream
    work — removed.
    """
    if not name:
        return []
    base = name.strip()
    if len(base) < 3:
        return [base]
    words = base.split()
    if len(words) == 1:
        vs = _word_variants(words[0], cap=cap)
        # vs is lowercase (e.g. ["arun", "aarun"]); promote each to
        # title-case. Original `base` (which may be mixed-case) is kept
        # first so the user's exact input is the lead variant.
        final = [base]
        seen = {base}
        for v in vs:
            titled = v.title()
            if titled not in seen:
                seen.add(titled)
                final.append(titled)
                if len(final) >= cap:
                    return final
        return final

    per_word_cap = max(3, cap // len(words) + 1)
    word_options = [_word_variants(w, cap=per_word_cap) for w in words]

    combos = [""]
    for opts in word_options:
        nxt = []
        for prefix in combos:
            for opt in opts:
                nxt.append((prefix + " " + opt).strip() if prefix else opt)
        combos = nxt

    # Promote each combo to title-case.
    final = [base]
    seen = {base}
    for c in combos:
        titled = c.title()
        if titled not in seen:
            seen.add(titled)
            final.append(titled)
            if len(final) >= cap:
                return final
    return final
