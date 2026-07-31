# ADK evaluation

## Running

```
adk eval eval_agent evals/<file> --config_file_path=evals/test_config.json --print_detailed_results
```

Run from the project root, one file at a time. `eval_agent` is a thin wrapper
package (see `eval_agent/`) that exposes the real `root_agent` from the
project root's `agent.py` under the module name ADK expects; nothing about
the agent itself is duplicated or changed.

`routing/routing_classification.test.json` and `drafting/drafting_rejection.test.json`
each use their own config, since both omit `tool_trajectory_avg_score` (see
the per-case notes below):

```
adk eval eval_agent evals/routing/routing_classification.test.json --config_file_path=evals/routing/test_config.json --print_detailed_results
adk eval eval_agent evals/drafting/drafting_rejection.test.json --config_file_path=evals/drafting/test_config.json --print_detailed_results
```

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
| `tracker_staging.test.json` | `tracker_stages_without_committing` | A tracker update request calls `tracker_agent` once, stages new entries, and never commits them — the confirm-before-write guarantee. |
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

Routing and drafting are both now judged on response content only
(`final_response_match_v2`), not trajectory — see the per-case notes below
for why exact trajectory matching was unsuitable for each regardless of the
fixes above.

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

## What it does NOT assert

None of these evals assert that extracted roles, companies, statuses, or
dates are correct. `final_response_match_v2` is an LLM-judged comparison
against the reference response, which is a useful but imperfect signal (see
the `drafting/drafting_rejection.test.json` false-positive note above) — it is not a
substitute for reading `actual_response` on a failing or suspicious run.
Correctness of extracted statuses and dates is covered instead by
`tests/test_write_to_sheet.py`.
