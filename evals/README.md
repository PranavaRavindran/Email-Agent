# ADK evaluation

## Running

The standard way to run the full suite is `./run_evals.sh` from the project
root. It runs all 5 cases in sequence with their correct config files (the
pairings below), prints a banner before each case, continues through all 5
even if one fails, and prints a pass/fail summary table at the end. Pass a
short case name to run just one, e.g. `./run_evals.sh drafting`. It exits
early with a clear message if `GOOGLE_API_KEY` is unset. `run_evals.sh` is
deliberately not part of CI — see "Why this isn't run automatically" below.

The individual `adk eval` invocations it wraps, for reference or manual use:

```
adk eval eval_agent evals/inbox_listing.test.json --config_file_path=evals/test_config.json --print_detailed_results
adk eval eval_agent evals/tracker_preview.test.json --config_file_path=evals/test_config.json --print_detailed_results
adk eval eval_agent evals/routing/routing_classification.test.json --config_file_path=evals/routing/test_config.json --print_detailed_results
adk eval eval_agent evals/drafting/drafting_rejection.test.json --config_file_path=evals/drafting/test_config.json --print_detailed_results
adk eval eval_agent evals/tracker/tracker_staging.test.json --config_file_path=evals/tracker/test_config.json --print_detailed_results
```

Run from the project root. `eval_agent` is a thin wrapper package (see
`eval_agent/`) that exposes the real `root_agent` from the project root's
`agent.py` under the module name ADK expects; nothing about the agent itself
is duplicated or changed.

`routing/routing_classification.test.json`, `drafting/drafting_rejection.test.json`,
and `tracker/tracker_staging.test.json` each use their own config, since all
three omit `tool_trajectory_avg_score` (see the per-case notes below).
`inbox_listing.test.json` and `tracker_preview.test.json` use the shared
`evals/test_config.json`.

## Why this isn't run automatically

These evals make real Gmail API calls (via `search_email_ids`,
`search_emails`, `list_emails`, and `get_email_detail`) and, for the tracker
cases, a real Google Sheets read (via `stage_write`). They're slow and depend
on live inbox contents, so they're run deliberately by a person rather than
on every change, unlike the fast, mocked tests in `tests/`.

`root_agent` and `tracker_agent` both run at `temperature=0.0` to keep tool
call arguments as reproducible as possible across runs, since
`tool_trajectory_avg_score` does an exact match on tool names AND args. This
helps but does not fully eliminate run-to-run variance — see the per-case
notes below.

## Eval cases

| File | eval_id | Asserts |
| --- | --- | --- |
| `tracker/tracker_staging.test.json` | `tracker_stages_without_committing` | A tracker update request calls `tracker_agent` once, stages new entries, and never commits them — the confirm-before-write guarantee. Judged with `tracker/test_config.json` (`final_response_match_v2` only — no trajectory score; see note below). |
| `inbox_listing.test.json` | `inbox_lists_recent_emails` | A plain "show me my recent emails" request is answered by a single `inbox_agent` call — no unnecessary routing through classification. |
| `routing/routing_classification.test.json` | `attention_request_routes_through_classification` | A "what needs my attention" request produces a response with emails grouped into priority categories (Urgent, Action Needed, FYI) — output only `classification_agent`'s involvement can produce, since `inbox_agent` alone would return a flat, uncategorized list. Judged with `routing/test_config.json` (`final_response_match_v2` only — no trajectory score; see note below). |
| `drafting/drafting_rejection.test.json` | `rejection_reply_does_not_express_continued_interest` | A reply drafted to a rejection email is a gracious acknowledgement that does NOT express continued interest in the declined role or ask about next steps for it. Guards against the drafting agent judging outcome from the subject line instead of the body. This is the most valuable case in the set. Judged with `drafting/test_config.json` (`final_response_match_v2` only — no trajectory score; see note below). |
| `tracker_preview.test.json` | `preview_does_not_stage` | A preview request ("show me what you'd add, but don't write anything") calls `tracker_agent` once and the response explicitly states nothing was staged or written. |

## Known live-run failures (historical)

The two substance-level bugs previously tracked here are both now fixed:

- **`drafting/drafting_rejection.test.json`**: `root_agent` previously asked
  `inbox_agent` for the "most recent email from Cisco" without requesting
  `get_email_detail` explicitly (contradicting its own chaining
  instructions), got back only the subject line, and fabricated an intent of
  "express continued interest" for what is actually a rejection email —
  producing a reply containing "I remain very interested in this
  opportunity", the exact regression this case exists to catch. This
  delegation bug is fixed: `root_agent` now retrieves the full email body
  and passes an accurate intent (e.g. "gracious acknowledgement of job
  application rejection") to `drafting_agent`. Notably,
  `final_response_match_v2` (the LLM judge) scored that old buggy reply as a
  1.0 match against the correct reference, so a PASSED score on this metric
  alone should not be trusted here — always read `actual_response`. The case
  now lives in `evals/drafting/` and is judged with `drafting/test_config.json`
  (`final_response_match_v2` only, no trajectory score — see below).
- **`tracker_preview.test.json`**: across three live runs, the final
  response never explicitly stated that nothing was staged or written, and
  one run's response used stage-diff vocabulary ("I found 34 new entries and
  no status changes... would you like to write these to the sheet?") that
  `tracker_agent`'s own instructions reserve for INTENT 2 (search-and-stage),
  suggesting `stage_write` may have actually run despite the user saying
  "don't write anything". This preview-staging bug is also fixed.

Routing, drafting, and tracker staging are all now judged on response content
only (`final_response_match_v2`), not trajectory — see the per-case notes
below for why exact trajectory matching was unsuitable for each regardless of
the fixes above.

`routing/routing_classification.test.json`'s problem was never about
`root_agent`'s behavior — it was that `classification_agent`'s args embed
`inbox_agent`'s freeform per-email summary of live inbox contents, which
varies run to run regardless of temperature, so `tool_trajectory_avg_score`
could never reliably pass for this case no matter how correct the routing
was. It lives in `evals/routing/` with its own `test_config.json` that omits
`tool_trajectory_avg_score` and scores only `final_response_match_v2`; see
the case's `description` field and the table entry above for what the
reference response asserts instead.

`drafting/drafting_rejection.test.json`'s trajectory assertion was likewise
removed regardless of the delegation fix above: `drafting_agent`'s args
embed the full email body and a freeform intent phrase, neither of which is
reliably reproducible across runs. It lives in `evals/drafting/` with its
own `test_config.json`, scoring only `final_response_match_v2`; see the
case's `description` field for the full detail, including the false-positive
note on the old buggy reply.

`tracker/tracker_staging.test.json`'s trajectory assertion was removed for a
similar reason: `root_agent` paraphrases the user's request when delegating
to `tracker_agent` (e.g. "INTENT 2: Stage a write to the job application
tracker with application emails from June and July 2026" instead of the
verbatim request), so an exact match on the `request` arg passed to
`tracker_agent` cannot reliably pass. It lives in `evals/tracker/` with its
own `test_config.json`, scoring only `final_response_match_v2`; see the
case's `description` field for detail. The staged-not-written assertion
rests entirely on the judged response.

## What the trajectory assertion can and can't see

`root_agent`'s tool trajectory only shows which sub-agents it called
(`inbox_agent`, `classification_agent`, `drafting_agent`, `tracker_agent`)
and the `request` string passed to each — it does NOT show which tools a
sub-agent used internally. For example, `tracker_agent` may or may not have
called `stage_write` during a given invocation, but that is invisible to
`root_agent`'s trajectory either way. Any assertion about tool use *inside*
a sub-agent (e.g. "the preview case must not call `stage_write`") has to be
made through the judged final response instead of the trajectory — see
`tracker_preview.test.json` for the clearest example of this limitation.

## Metrics in use

| Metric | Measures | Used in |
| --- | --- | --- |
| `final_response_match_v2` | LLM-judged similarity of the final response to a fixed reference response. | All 5 cases. |
| `tool_trajectory_avg_score` | Exact match of tool names and args against a reference trajectory. | `inbox_listing.test.json`, `tracker_preview.test.json` only (via `evals/test_config.json`) — see "What the trajectory assertion can and can't see" above and the rejected-alternative note below for why the other three cases don't use it. |
| `rubric_based_final_response_quality_v1` | LLM-judged pass/fail against a list of specific, independent yes/no criteria (rubrics) about the final response — not similarity to any reference text. | `drafting/drafting_rejection.test.json`, alongside `final_response_match_v2`. |
| `rubric_based_tool_use_quality_v1` | Same rubric mechanism as above, applied to the agent's tool-use behavior rather than its final text. | `drafting/drafting_rejection.test.json`. |
| `hallucinations_v1` | LLM-judged check of whether claims made in the response(s) are actually grounded in the tool outputs the agent had access to. | `tracker/tracker_staging.test.json`. |

**Why rubric-based scoring was added:** see "Known live-run failures
(historical)" above — `final_response_match_v2` scored the old, buggy
"continued interest" reply as a **1.0 match** against the correct reference.
That's not a fluke of that one run; it's structural. Similarity-to-reference
and satisfaction-of-a-requirement are different questions, and a response can
score high on the first while failing the second: the buggy reply matched
the reference in tone, length, structure, and topic, and differed by exactly
one clause that inverted the meaning — precisely the kind of difference a
similarity judge is bad at weighting. `rubric_based_final_response_quality_v1`
asks a different question per rubric ("does the reply avoid expressing
continued interest in the declined role?") instead of "how similar is this
text to the reference?", so it can fail a response for the one clause that
matters even when everything else matches. `final_response_match_v2` is kept
on this case rather than replaced, so the contrast between the two scores on
the same run stays visible — a case where they diverge is itself a signal
worth reading.

The rubric and hallucination metric thresholds added in this pass are set to
`0.8`, matching the `final_response_match_v2` threshold already used
throughout `evals/`, for consistency — nothing in the criterion schema
(`RubricsBasedCriterion` / `HallucinationsCriterion` in
`google/adk/evaluation/eval_metrics.py`) requires a different value; `0.8`
was not derived from any calibration run, since no eval has actually been
executed as part of this work (see the repository's eval-execution
constraints).

**IN_ORDER / ANY_ORDER trajectory matching was evaluated and rejected** as a
fix for the exact-match brittleness described above and in the per-case
notes. `ToolTrajectoryCriterion.MatchType.IN_ORDER` and `.ANY_ORDER` only
relax the *ordering* requirement and allow *extra* calls around the expected
ones — they do not relax the argument comparison itself.
`trajectory_evaluator.py`'s `_are_tool_calls_in_order_match` (`actual.args ==
current_expected.args`, line ~196) and `_are_tool_calls_any_order_match`
(`actual.name == expected.name and actual.args == expected.args`, line
~232) both still require an exact args match for any call they consider
"matched" to an expected one. Since `drafting_agent`'s and `tracker_agent`'s
args embed freeform, per-run content (full email bodies, freeform intent
phrases, paraphrased requests), no match type in this evaluator can pass
reliably for those two cases — the problem is the args comparison, not the
ordering constraint. This is why rubric-based and hallucination metrics
(LLM-judged, not exact-matched) were added instead of loosening the match
type.

## Observability

`check_drift.py`, at the project root, is **observability, not evaluation**.
Evals in this directory check the agent's behavior against cases written in
advance — a fixed set of inputs with a known-good reference response.
`check_drift.py` instead inspects `run_log.jsonl`, the record `stage_write`
appends on every real invocation (see
`tools/write_to_sheet.py::_log_run`), including runs nobody wrote a case for.
Evals answer "does the agent still pass the scenarios we thought of";
`check_drift.py` answers "did anything strange happen in production."

It flags two independent drift signals per record — fewer emails fetched
than searched, and more entries staged than emails fetched — and treats
either one occurring alongside `refused == false` as the most severe
verdict, `GUARD DID NOT FIRE`, since `stage_write` has a code-level guard
that's supposed to catch exactly that. Run it with `python check_drift.py`
(optionally `--last N`). See its module docstring for full detail, including
why the committed `run_log.jsonl` contains a historical record it is
*expected* to flag.

## Known gaps

- `inbox_listing.test.json` and `tracker_preview.test.json` have no rubric
  coverage — they're still judged only by `tool_trajectory_avg_score` and
  `final_response_match_v2` (via the shared `evals/test_config.json`).
- `routing/routing_classification.test.json` has no path/trajectory
  coverage at all — see "What the trajectory assertion can and can't see"
  above for why, and it also has no rubric-based coverage added in this
  pass.

## What it does NOT assert

None of these evals assert that extracted roles, companies, statuses, or
dates are correct. `final_response_match_v2` is an LLM-judged comparison
against the reference response, which is a useful but imperfect signal (see
the `drafting/drafting_rejection.test.json` false-positive note above) — it is not a
substitute for reading `actual_response` on a failing or suspicious run.
Correctness of extracted statuses and dates is covered instead by
`tests/test_write_to_sheet.py`.
