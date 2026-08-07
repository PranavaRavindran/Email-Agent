# Code Review — Phase 1 Quality Pass

This document was written before any fix in this section was applied, as an
audit of the concurrency and tool-schema surface of the codebase. Section 1
is ranked by severity; the two fixes it calls for are applied in the same
commit that adds this file, immediately after the audit below.

## Section 1 — Thread safety audit

### 1. `auth.py::get_gmail_service` — unguarded lazy init on a module global (fixed)

```python
def get_gmail_service():
    global _service
    if _service is None:
        _service = _build_service()
    return _service
```

`get_thread_local_gmail_service` (used by every worker thread in
`get_emails_bulk`'s pool) calls this once per thread to obtain the shared
credentials object (`get_gmail_service()._http.credentials`). `main.py`
calls `initialize_gmail_service()` eagerly before the thread pool can ever
run, so the CLI entry point never actually races here. But `eval_agent/agent.py`
and `adk web` do not go through `main.py` — under those entry points,
`_service` is still `None` when the first `get_emails_bulk` call spins up its
8-thread pool, and all 8 threads can race through the `if _service is None`
check simultaneously. Losers of the race still call `_build_service()`,
which can reach `flow.run_local_server()` (spawning a local OAuth server /
browser flow) and `pickle.dump(creds, f)` (writing `token.pickle`) more than
once concurrently — a torn write to `token.pickle` corrupts the saved
credentials for every future run, not just the current one.

**Fix applied:** a module-level `threading.Lock` around `_build_service()`
in `get_gmail_service`, with double-checked locking (`_service` is
re-checked once inside the lock, so only the thread that actually loses the
race pays for acquiring it, and `_service` is still only built once).
Public signatures are unchanged; `threading` was already imported.

### 2. `tools/get_email_detail.py` — `_FETCHED_IDS` / `_BODY_CACHE` module globals (no change)

Both are module-level mutable containers written from every pool thread in
`_fetch_one`: `_FETCHED_IDS.add(email_id)` and `_BODY_CACHE[email_id] = result`.
Neither is lock-protected. `set.add` and `dict.__setitem__` on `str` keys are
effectively atomic under CPython's GIL — a thread performing one of these
operations cannot be interrupted mid-operation by another thread doing the
same operation on the same container, so this does not tear or corrupt the
containers. **This is documented here as an implementation detail of
CPython, not a language guarantee** — it does not hold on interpreters
without a GIL (e.g. `--disable-gil` builds on 3.13+, or non-CPython
runtimes), and should be revisited if the project ever moves off stock
CPython. No code change made.

### 3. `tools/get_emails_bulk.py` — no id dedup; unlocked check-then-act in `_fetch_one` (partially fixed)

`get_emails_bulk` did not deduplicate `email_ids` before splitting them into
`cached_ids` / `uncached_ids` and submitting each uncached id to the thread
pool:

```python
future_to_id = {executor.submit(_fetch_one, email_id, True): email_id for email_id in uncached_ids}
```

If the caller passes a duplicate id, it appears twice in `uncached_ids`, so
two threads independently call `_fetch_one` for the same id. `_fetch_one`'s
own cache check (`if email_id in _BODY_CACHE: ... return _BODY_CACHE[email_id]`)
is an unlocked check-then-act, so both threads can pass the check before
either writes the cache and both end up fetching the same email over the
network. This is **inefficiency, not incorrectness** — both threads compute
and return the identical result (the API call is idempotent and the final
`_BODY_CACHE[email_id] = result` write is the same value regardless of which
thread's fetch "wins"), so no caller ever observes a wrong answer, just a
wasted API call.

**Fix applied:** deduplicate `email_ids` preserving order at the top of
`get_emails_bulk` via `dict.fromkeys(email_ids)`, so no id is ever submitted
to the pool twice from a single `get_emails_bulk` call. **`_fetch_one` itself
is intentionally left unlocked** — adding a lock there would serialize the
one part of the pipeline (the network fetch) that benefits from staying
concurrent, in exchange for closing a residual race that only matters across
*separate, overlapping* `get_emails_bulk` calls sharing the same uncached id,
which is not a case this codebase's call patterns produce.

### 4. `tools/get_email_detail.py` — fetch-then-record ordering (confirmed correct, no change)

```python
_FETCHED_IDS.add(email_id)
result = {...}
_BODY_CACHE[email_id] = result
return result
```

`_FETCHED_IDS.add(email_id)` is only reached after `.execute()` has already
returned successfully (it sits after the Gmail API call, not before it) —
confirmed by reading the function top to bottom: the `.execute()` call and
body extraction happen first, and `_FETCHED_IDS.add` follows. A failed fetch
(an exception raised out of `.execute()`) therefore is never recorded as
"fetched." This is explicitly **load-bearing** for `stage_write`'s
completeness guard, which refuses to write if fewer ids were fetched than
`search_email_ids` returned — if a failed fetch were still marked as
fetched, that guard would silently pass over emails that were never actually
read. This exact behavior is asserted by
`test_partial_failure_returns_error_entry_and_continues` in
`tests/test_get_emails_bulk.py`. No change made; explicitly confirmed as
correct by this audit.

## Section 2 — Why the test suite cannot catch these

Every test that exercises `get_emails_bulk`'s thread pool monkeypatches
`get_thread_local_gmail_service` (via `ged.get_thread_local_gmail_service`,
see `tests/test_get_emails_bulk.py`) with a lambda that constructs a brand
new, independent fake service object on *every call*:

```python
monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))
```

There is no shared, mutable resource under test — no real `_service`
singleton, no real `token.pickle`, no real lock to race on. Each of the 8
pool threads gets its own throwaway fake, so a test can never observe the
lazy-init race in `get_gmail_service`, no matter how it's structured. This
is a structural blind spot, not a coverage gap that more tests would close:
the thing being raced on (module-global lazy init reaching disk I/o and a
network OAuth flow) is exactly the thing the tests replace with a fake to
keep themselves fast and deterministic.

The same blindness applies, for a different reason, to tool schema changes
(see Section 3): tests call tool functions directly as plain Python
functions with monkeypatched services. ADK's schema generation
(`google/adk/tools/_function_parameter_parse_util.py`, which inspects a
function's type annotations to build the JSON schema sent to Gemini) never
executes anywhere in the pytest run, so a parameter annotation change that
alters what Gemini receives produces no test failure either way.

## Section 3 — Tool schema gaps

There are 10 functions registered as ADK tools across 7 files
(`write_to_sheet.py` alone registers three: `preview_resolve`, `stage_write`,
`commit_write`), one more than the "nine model-facing tools" this task's
scoping list enumerates — noted here rather than silently corrected. The
table below covers all 10 for completeness. For each, this is the current
parameter annotation, the
schema ADK generates from it today (per the bare-`list`-vs-`list[T]`
behavior verified in ADK 2.4.0's
`_function_parameter_parse_util.py`), and what a `TypedDict`-based
annotation would add. **No schema or parameter-shape changes are made in
this pass** — this section is a survey, and any such change is explicitly
deferred to Phase 2, gated on eval coverage that could actually detect a
regression in what gets sent to Gemini (see Section 2).

| Tool | Current annotation | Schema ADK generates today | What a TypedDict would add |
|---|---|---|---|
| `list_emails` | `max_results: int = 20` | `{"type": "INTEGER"}`, optional | N/A — no `dict`/`list` param |
| `search_emails` | `query: str, max_results: int = 20` | `STRING`, `INTEGER` | N/A |
| `search_email_ids` | `query: str, max_results: int = 100` | `STRING`, `INTEGER` | N/A |
| `get_email_detail` | `email_id: str` | `STRING` | N/A |
| `get_emails_bulk` | `email_ids: list[str]` (widened this pass) | `{"type": "ARRAY", "items": {"type": "STRING"}}` | Already fully precise — items are plain strings, nothing left for a TypedDict to add |
| `classify_email` | `email: dict` | `{"type": "OBJECT"}` with no declared properties — Gemini sees an opaque blob | A `TypedDict` (`from_`, `subject`, `body`, `date`) would populate `properties`, telling Gemini exactly which keys to expect/construct instead of guessing the shape from prose in the agent instructions |
| `draft_reply` | `email: dict, intent: str` | `email` → OBJECT, no properties; `intent` → plain `STRING`, no enum | Same OBJECT gap as `classify_email` for `email`; note `intent`'s small fixed value set isn't a TypedDict concern at all — that's a `Literal`/enum gap, separate from this survey |
| `preview_resolve` | `entries: list[dict]` (widened this pass) | `{"type": "ARRAY", "items": {"type": "OBJECT"}}` — items are typed as objects now, but still no per-item field schema | A `TypedDict` per entry (`date`, `company`, `role`, `source`, `status`) would populate `items.properties`, so a malformed entry (missing `role`, wrong key name) could in principle be caught by Gemini's own generation constraints rather than surfacing only as a runtime validation error from `_validate_entry` |
| `stage_write` | `entries: list[dict]` (widened this pass) | Same as `preview_resolve` | Same as `preview_resolve` |
| `commit_write` | *(no params)* | N/A | N/A |

The `list` → `list[str]` / `list[dict]` widenings applied earlier in this
pass are a prerequisite for any of the `TypedDict` work above — Gemini's
schema for a bare `list` has no `items` field at all, so today's ADK-generated
JSON gives the model no signal that entries are even objects, let alone what
fields they should carry.

## Section 4 — Complexity report

`radon cc -s -a tools/ agents/ auth.py main.py agent.py`:

```
tools/find_application_date.py
    F 439:0 _select_confirmation - C (19)
    F 197:0 _role_matches - B (7)
    F 234:0 _outcome_role_matches - B (7)
    F 278:0 _no_identifying_info - B (7)
    F 631:0 find_application_date - B (7)
    F 70:0 _sought_identifier - A (5)
    F 100:0 _significant_words - A (5)
    F 108:0 _role_word_match - A (5)
    F 315:0 _classify_intent - A (4)
    F 590:0 _fetch_candidates - A (4)
    F 59:0 _extract_req_number - A (3)
    F 64:0 _extract_identifiers - A (3)
    F 83:0 _identifier_verdict - A (3)
    F 131:0 _same_role - A (3)
    F 146:0 _role_hint - A (3)
    F 169:0 _extract_role - A (3)
    F 421:0 is_confirmation_email - A (3)
    F 715:0 _parse_header_date - A (3)
    F 50:0 _company_token - A (2)
    F 183:0 _normalize_for_match - A (2)
    F 187:0 _contains_role - A (2)
    F 433:0 _looks_like_confirmation - A (2)
    F 55:0 _parse_date - A (1)
tools/classify_email.py
    F 8:0 classify_email - A (4)
tools/get_emails_bulk.py
    F 8:0 get_emails_bulk - B (10)
tools/search_emails.py
    F 4:0 search_emails - A (4)
tools/draft_reply.py
    F 6:0 draft_reply - A (2)
tools/write_to_sheet.py
    F 62:0 _validate_entry - D (23)
    F 374:0 _resolve - C (20)
    F 616:0 stage_write - C (13)
    F 287:0 _merge_duplicates - C (12)
    F 226:0 _resolve_dates - B (6)
    F 168:0 _company_matches - A (5)
    F 569:0 preview_resolve - A (5)
    F 191:0 _fetch_source_emails - A (4)
    F 44:0 _normalize - A (3)
    F 542:0 _apply - A (3)
    F 765:0 commit_write - A (3)
    F 134:0 _log_run - A (2)
    F 180:0 _keys_match - A (2)
    F 184:0 _sort_key - A (2)
    F 206:0 _has_observed_confirmation - A (2)
    F 216:0 _group_source_text - A (2)
tools/get_email_detail.py
    F 42:0 _fetch_one - B (7)
    F 96:0 _find_part_data - A (5)
    F 126:0 _extract_body - A (3)
    F 116:0 _html_to_text - A (2)
    F 18:0 get_fetched_ids - A (1)
    F 23:0 reset_fetched_ids - A (1)
    F 29:0 get_email_detail - A (1)
    F 112:0 _decode - A (1)
tools/list_emails.py
    F 4:0 list_emails - A (4)
tools/search_email_ids.py
    F 25:0 search_email_ids - A (5)
    F 14:0 get_last_search_count - A (1)
    F 19:0 get_last_search_range - A (1)
auth.py
    F 62:0 _build_service - B (7)
    F 28:0 get_gmail_service - A (3)
    F 37:0 get_thread_local_gmail_service - A (2)
    F 55:0 initialize_gmail_service - A (1)
main.py
    F 16:0 run_agent - C (11)
    F 60:0 main - A (2)

61 blocks (classes, functions, methods) analyzed.
Average complexity: A (4.721311475409836)
```

`radon mi tools/ agents/` — every module scores grade **A**:

```
tools/find_application_date.py - A
tools/classify_email.py - A
tools/get_emails_bulk.py - A
tools/__init__.py - A
tools/search_emails.py - A
tools/draft_reply.py - A
tools/write_to_sheet.py - A
tools/get_email_detail.py - A
tools/list_emails.py - A
tools/search_email_ids.py - A
agents/classification_agent.py - A
agents/tracker_agent.py - A
agents/__init__.py - A
agents/inbox_agent.py - A
agents/drafting_agent.py - A
```

Two functions stand out from the rest of the distribution (average cyclomatic
complexity across all 61 analyzed blocks is 4.7, solidly grade A):

- **`_validate_entry` — D (23).** This is a flat validator: a long sequence
  of independent `if` checks, each rejecting the entry for one specific
  reason (missing field, bad date format, unknown status value, etc.) and
  returning immediately. The high count is a direct reflection of how many
  distinct rejection reasons `stage_write`'s contract requires distinguishing
  between, not of tangled control flow — there's no nesting depth or
  interacting state to simplify. **Deliberately not refactored.** Splitting
  it into smaller functions would trade one long, linearly-readable
  validator for several short ones plus the indirection of following calls
  between them, for a construct that has no shared state between checks to
  hide bugs in.

- **`_select_confirmation` — C (19).** Unlike `_validate_entry`, this one is
  **genuinely complex**: real nesting (loop over candidates, nested
  conditionals per candidate), loop-carried state (`weak_match`, updated
  conditionally across iterations and read after the loop via
  `_end_of_scan`), and a network call (`get_email_detail`) made mid-branch
  only when a candidate's body isn't already available. This is the one
  candidate in the codebase flagged here for future restructuring — for
  example, separating "fetch the candidate's body if needed" from "decide
  whether this candidate resolves the search" would remove the interleaving
  of I/O and control flow that makes this function harder to reason about
  than its complexity number alone suggests. **Not refactored in this
  pass** — this report is observational only.

No refactoring was performed as part of this section.
