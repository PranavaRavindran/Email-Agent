# ADK evaluation

## Running

The standard way to run the full suite is `./run_evals.sh` from the project
root. It runs all 5 cases in sequence with their correct config files (the
pairings below), prints a banner before each case, continues through all 5
even if one fails or errors, and prints a pass/fail summary table at the
end. Pass a short case name to run just one, e.g. `./run_evals.sh drafting`.
It exits early with a clear message if `GOOGLE_API_KEY` is unset.
`run_evals.sh` is deliberately not part of CI — see "Why this isn't run
automatically" below. The one exception to "continues through all 5" is API
quota exhaustion — see "Quota exhaustion aborts the run" below.

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

## Quota exhaustion aborts the run

`run_evals.sh` scans each case's captured output for `429` and
`RESOURCE_EXHAUSTED` together — the signature of a depleted Gemini API
quota — and if it finds both, prints an `ABORTING` message to stderr and
exits immediately without running the remaining cases. This is deliberate:
a depleted quota does not recover mid-run, and letting the loop continue
means every remaining case burns roughly 20 minutes on exponential backoff
before its own call finally gives up. On the 2026-08-08 run this is exactly
what happened to `tracker/tracker_staging.test.json` and
`tracker_preview.test.json` — quota was exhausted partway through, and both
cases ran their full backoff cycle for nothing.

Separately, `run_evals.sh` treats a case as `ERROR` (not `FAILED` or
`PASSED`) whenever its output contains `429` or `NOT_EVALUATED`, even if ADK
also printed `Overall Eval Status: FAILED` for that case. On the same
2026-08-08 run, `tracker` and `tracker_preview` were reported `FAILED` by
ADK's own verdict line despite inference never actually running for the
metric in question — a `NOT_EVALUATED` metric status is not a failed
assertion, and treating it as `FAILED` in the summary table would read as
"the agent produced the wrong output" when what actually happened is "the
judge never ran." `ERROR` makes that distinction visible in the summary
without requiring a re-read of the full log to catch it.

## Eval cases

| File | eval_id | Asserts |
| --- | --- | --- |
| `tracker/tracker_staging.test.json` | `tracker_stages_without_committing` | A tracker update request calls `tracker_agent` once, stages new entries, and never commits them — the confirm-before-write guarantee. Judged with `tracker/test_config.json`: `rubric_based_final_response_quality_v1` (states entries were staged, states nothing written yet, asks for confirmation) is the primary signal; `final_response_match_v2` runs at the standard `0.8` threshold against a shape-based reference — see "Stale references aren't just a trajectory problem" below. No trajectory score; see note below. |
| `inbox_listing.test.json` | `inbox_lists_recent_emails` | A plain "show me my recent emails" request is answered by a single `inbox_agent` call — no unnecessary routing through classification. |
| `routing/routing_classification.test.json` | `attention_request_routes_through_classification` | A "what needs my attention" request produces a response with emails grouped into priority categories (Urgent, Action Needed, FYI) — output only `classification_agent`'s involvement can produce, since `inbox_agent` alone would return a flat, uncategorized list. Judged with `routing/test_config.json`: `rubric_based_final_response_quality_v1` (categorized structure without requiring every category populated, Urgent means a job-search deadline, security alerts/verification codes are NOT Urgent, rejections/marketing not Action Needed, like emails categorised consistently, all emails accounted for) is the primary signal; `final_response_match_v2` runs at the standard `0.8` threshold against a shape-based reference — see "Stale references aren't just a trajectory problem" below. No trajectory score; see note below. |
| `drafting/drafting_rejection.test.json` | `rejection_reply_does_not_express_continued_interest` | A reply drafted to a rejection email is a gracious acknowledgement that does NOT express continued interest in the declined role or ask about next steps for it. Guards against the drafting agent judging outcome from the subject line instead of the body. This is the most valuable case in the set. Judged with `drafting/test_config.json`: `final_response_match_v2`, `rubric_based_final_response_quality_v1`, and `rubric_based_tool_use_quality_v1` — no trajectory score; see note below. |
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
  (`final_response_match_v2` plus rubric-based metrics, no trajectory score
  — see below).
- **`tracker_preview.test.json`**: across three live runs, the final
  response never explicitly stated that nothing was staged or written, and
  one run's response used stage-diff vocabulary ("I found 34 new entries and
  no status changes... would you like to write these to the sheet?") that
  `tracker_agent`'s own instructions reserve for INTENT 2 (search-and-stage),
  suggesting `stage_write` may have actually run despite the user saying
  "don't write anything". This preview-staging bug is also fixed.

On the 2026-08-08 live run, two more failures surfaced, both false failures
against stale reference responses rather than agent bugs — see "Stale
references aren't just a trajectory problem" below for the fix:

- **`routing/routing_classification.test.json`** scored `0.0` on
  `final_response_match_v2` while producing exactly the grouping the case
  asserts (Urgent / Action Needed / a residual bucket). The reference
  response recorded in the `.test.json` names an email ("Quarterly Report
  Due from jane@example.com") that doesn't exist in the live inbox the run
  was scored against, so the similarity judge was comparing correct output
  to emails that no longer exist.
- **`tracker/tracker_staging.test.json`** scored `0.0` on the same metric
  for the same reason, while `hallucinations_v1` — which checks grounding,
  not resemblance to a fixed reference — scored a full `1.0` on that run.

Separately, the same run's judge flagged (unprompted, since no rubric
covered it) that the Cisco email in `drafting/drafting_rejection.test.json`
states "This is a post-only email. Please do not reply", yet the agent
drafted a reply anyway. `rubric_based_final_response_quality_v1` for that
case now includes a rubric for this.

Routing, drafting, and tracker staging are all now judged on response
content, not trajectory — see the per-case notes below for why exact
trajectory matching was unsuitable for each regardless of the fixes above.
Routing and tracker staging combine `final_response_match_v2` with
`rubric_based_final_response_quality_v1` (see "Stale references aren't just
a trajectory problem" below); drafting combines it with
`rubric_based_final_response_quality_v1` and `rubric_based_tool_use_quality_v1`.

`routing/routing_classification.test.json`'s problem was never about
`root_agent`'s behavior — it was that `classification_agent`'s args embed
`inbox_agent`'s freeform per-email summary of live inbox contents, which
varies run to run regardless of temperature, so `tool_trajectory_avg_score`
could never reliably pass for this case no matter how correct the routing
was. It lives in `evals/routing/` with its own `test_config.json` that omits
`tool_trajectory_avg_score`; see the case's `description` field and the
table entry above for what the reference response and rubrics assert
instead.

`drafting/drafting_rejection.test.json`'s trajectory assertion was likewise
removed regardless of the delegation fix above: `drafting_agent`'s args
embed the full email body and a freeform intent phrase, neither of which is
reliably reproducible across runs. It lives in `evals/drafting/` with its
own `test_config.json`, scoring `final_response_match_v2` alongside its
rubric-based metrics; see the case's `description` field for the full
detail, including the false-positive note on the old buggy reply.

`tracker/tracker_staging.test.json`'s trajectory assertion was removed for a
similar reason: `root_agent` paraphrases the user's request when delegating
to `tracker_agent` (e.g. "INTENT 2: Stage a write to the job application
tracker with application emails from June and July 2026" instead of the
verbatim request), so an exact match on the `request` arg passed to
`tracker_agent` cannot reliably pass. It lives in `evals/tracker/` with its
own `test_config.json`; see the case's `description` field for detail. The
staged-not-written assertion rests entirely on the judged response — now
primarily its `rubric_based_final_response_quality_v1` rubrics rather than
`final_response_match_v2`, per "Stale references aren't just a trajectory
problem" below.

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
| `final_response_match_v2` | LLM-judged pass/fail of the final response against a reference response. Despite the "match" name this is not a continuous similarity score: it is **binary** — the judge outputs "valid" or "invalid" per invocation (0 or 1), and with one invocation per case in this suite the overall score can only be exactly `0.0` or `1.0`. See `google/adk/evaluation/final_response_match_v2.py:133-137`. | All 5 cases, threshold `0.8` throughout — see "Stale references aren't just a trajectory problem" below for why `routing/routing_classification.test.json` and `tracker/tracker_staging.test.json` use shape-based references rather than a lowered threshold. |
| `tool_trajectory_avg_score` | Exact match of tool names and args against a reference trajectory. | `inbox_listing.test.json`, `tracker_preview.test.json` only (via `evals/test_config.json`) — see "What the trajectory assertion can and can't see" above and the rejected-alternative note below for why the other three cases don't use it. |
| `rubric_based_final_response_quality_v1` | LLM-judged pass/fail against a list of specific, independent yes/no criteria (rubrics) about the final response — not similarity to any reference text. | `drafting/drafting_rejection.test.json`, `routing/routing_classification.test.json`, and `tracker/tracker_staging.test.json`, alongside `final_response_match_v2` on each. |
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

## Stale references aren't just a trajectory problem

"What the trajectory assertion can and can't see" above and the
`tool_trajectory_avg_score` per-case notes describe why exact trajectory
matching breaks when tool args embed live, per-run content. The same
structural problem also applies to `final_response_match_v2`, and the
2026-08-08 live run (see "Known live-run failures (historical)" above)
demonstrated it: `routing/routing_classification.test.json` and
`tracker/tracker_staging.test.json` both scored `0.0` while producing
correct output, because each case's `.test.json` reference response was
recorded against a specific inbox snapshot — it named actual senders and
subjects (e.g. "Quarterly Report Due from jane@example.com" in the routing
case) that existed at recording time. Once the live inbox moves on, a
correct response naming *today's* emails has essentially no lexical overlap
with a reference naming emails that no longer exist, and the similarity
judge scores it low regardless of whether the categorization or staging
behavior was correct. A fixed reference response is a snapshot of one
inbox's contents, not a specification of the behavior being tested — the
same distinction the trajectory notes above draw between "exact match to a
recorded call" and "the property that call needs to satisfy." On the same
run, `rubric_based_final_response_quality_v1` scored `1.0` on all four
routing rubrics, with the judge explicitly confirming the response was
correctly categorized — the clearest demonstration in this repo that
`final_response_match_v2` and rubric scoring can diverge on the *same*
response, and of why rubric scoring was added at all (see "Why rubric-based
scoring was added" above).

**First attempt, and why it didn't work.** The initial fix lowered
`final_response_match_v2`'s threshold to `0.3` on both cases, on the
assumption that this was a continuous similarity score and `0.3` would
absorb a partial-overlap penalty from stale wording while still catching a
truly wrong response. That assumption was wrong.
`final_response_match_v2` is not continuous — it is a **binary** metric.
The judge outputs "valid" or "invalid" per invocation, i.e. a score of `0`
or `1`; with repeated invocations the overall score is the fraction judged
valid, but this suite runs one invocation per case, so the only possible
scores are exactly `0.0` or `1.0`
(`google/adk/evaluation/final_response_match_v2.py:133-137`). A stale
reference doesn't produce a low-but-passing score to catch with a lowered
threshold — it produces `0.0`, and `0.0 >= 0.3` is exactly as false as
`0.0 >= 0.8`. The threshold change was inert on the failure it was meant to
fix.

**The actual fix: shape-based references.** The reference response for
both cases has been rewritten to describe the *shape* of a correct answer —
what structure it must have — rather than its *content* — which specific
emails it names. `routing/routing_classification.test.json`'s reference now
states it's a summary of emails needing attention, groups them into
priority categories, and accounts for the remaining lower-priority emails
without naming any of them individually. It does not name an Urgent
category, since the recorded inbox snapshot has no job-search deadline under
the adopted taxonomy (see "Eval artifacts encode the urgency taxonomy"
below) — a reference that hardcoded an Urgent example would itself go stale
the moment the taxonomy changed, which is exactly what happened once before.
`tracker/tracker_staging.test.json`'s reference states that new entries
were found and staged, that nothing has been written to the sheet yet, and
asks whether to write them — again with no specific company, role, date, or
count. Neither reference names anything that exists only in one inbox
snapshot, so neither goes stale as the inbox changes. With a shape-based
reference, `final_response_match_v2`'s threshold is restored to the
standard `0.8` on both cases — `0.8` is the correct value for a binary
metric precisely because it means "the judge must call this valid";
anything lower would accept a response the judge rejected.

**Why editing the reference was safe here.** Editing a `.test.json`'s
expected output is normally the wrong move — it's how a failing case gets
made to pass by moving the goalposts instead of fixing the agent. It's safe
in this specific instance only because the substantive assertions had
already been moved into `rubric_based_final_response_quality_v1` (unchanged
by this pass — see the case's `test_config.json`), and the reference
rewrite is strictly a reduction in specificity, not in what's required: the
new reference still demands the same structural properties (priority
grouping, security-alert placement, accounting for all emails; staged,
not-written, asks-to-confirm) that the rubrics also check, it just no
longer pins them to one inbox's contents. `final_response_match_v2` and
`rubric_based_final_response_quality_v1` are still kept in tension on both
cases — the same principle as "Why rubric-based scoring was added" above —
so a future divergence between them stays visible instead of being silently
resolved by dropping one metric.

`drafting/drafting_rejection.test.json`'s reference is presumably subject to
the same staleness risk in principle, but its case is anchored to a specific
named sender ("the most recent email from Cisco") rather than a snapshot of
the whole inbox, which has so far kept it stable; its threshold is left at
`0.8` for that reason. If it starts producing similar false failures against
live inbox drift, the same shape-based rewrite applies.

## Eval artifacts encode the urgency taxonomy

"Urgent" has a specific, non-obvious meaning in this project: a job-search
matter requiring action (interview scheduling, an assessment or take-home
with a due date, an application requiring information by a deadline) — not
generic time-sensitivity. That definition is encoded in **five** places, and
all five have to change together whenever the taxonomy changes:

1. `tools/classify_email.py`'s prompt (the governing principle, criteria, and
   examples the classification model actually sees).
2. `agents/classification_agent.py`'s instructions.
3. `agent_spec.yaml`'s category definitions.
4. The routing rubrics in `evals/routing/test_config.json`.
5. The reference response in `evals/routing/routing_classification.test.json`.

The commit that adopted this taxonomy (see `ENGINEERING_LOG.md` #23) updated
1–4 but missed 5, and also missed a second rubric in #4 that encoded a
different, older assumption ("Urgent must always be present") than the one
being fixed ("Urgent's content must be job-search deadlines, not generic
urgency"). The 2026-08-09 live run caught both misses as false failures:
`groups_into_distinct_priority_categories` scored `0.0` for correctly
omitting Urgent from a run with no job-search deadline, and
`final_response_match_v2` scored `0.0` because the reference response still
named a security alert and verification codes under Urgent — the exact
pre-decision taxonomy. See `ENGINEERING_LOG.md` #24 for the full account.
Neither the agent nor the rubric-content check was wrong; two artifacts that
encode the same specification had simply gone out of sync with each other.
When the taxonomy changes again, update all five, not just the ones that are
top of mind.

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
  above for why.

## What it does NOT assert

None of these evals assert that extracted roles, companies, statuses, or
dates are correct. `final_response_match_v2` is an LLM-judged comparison
against the reference response, which is a useful but imperfect signal (see
the `drafting/drafting_rejection.test.json` false-positive note above) — it is not a
substitute for reading `actual_response` on a failing or suspicious run.
Correctness of extracted statuses and dates is covered instead by
`tests/test_write_to_sheet.py`.
