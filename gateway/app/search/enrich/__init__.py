"""Gateway-layer modules that sit between the raw v4 upstream responses and
the public-facing API. Each module here is a pure-function helper:

  normalize    -> raw v4 case_history dict  -> stable CaseDetail shape
  confidence   -> query + case dict          -> 0-100 score + reasons
  sensitivity  -> case dict                  -> {is_sensitive, category, ...}
  sc_stub      -> CNR string                 -> {is_sc, deeplink} (until SC adapter ships)
  llm_variants -> name string                -> [variants] augmented by Gemini
  court_ranking-> courts + hint              -> reordered courts list

Nothing here owns persistent state; the existing reliability stack (cache,
coalescer, breaker, rate limiter) wraps these where appropriate.
"""
