# Investigation: tracker_preview returning zero results (2026-08-08 live run)

Scope: answer A1–A6 from the task with file:line or trajectory evidence for
every claim. Speculation is labelled explicitly as SPECULATION.

Primary sources read in full:
- `eval_agent/.adk/eval_history/eval_agent_tracker_preview_1786219152.1437619.evalset_result.json`
- `eval_agent/.adk/eval_history/eval_agent_tracker_staging_1786218893.67621.evalset_result.json`
- `agents/tracker_agent.py`, `agent.py`
- `tools/write_to_sheet.py`, `tools/get_email_detail.py`, `tools/search_email_ids.py`, `tools/get_emails_bulk.py`, `tools/find_application_date.py`
- `evals/tracker_preview.test.json`, `evals/tracker/tracker_staging.test.json`, `evals/tracker/test_config.json`, `evals/test_config.json`
- `run_log.jsonl`

**Important scope note discovered during investigation (applies to A2/A3):** the
two `.evalset_result.json` files only record **root_agent's own trajectory**
— for each case there are exactly 4 session events: the user message, one
`function_call` from root_agent to its `tracker_agent` AgentTool, one
`function_response` back, and root_agent's final text
(`eval_agent_tracker_preview_....json`, `session_details.events`, 4 entries;
same shape in the staging file). `tracker_agent`'s own internal steps
(`search_email_ids`, `get_emails_bulk`, model calls, `preview_resolve` /
`stage_write`) run **inside** that one root-level `function_call` →
`function_response` pair and are not logged as separate events in these
files at all, because `tracker_agent` is invoked via `AgentTool`, which
collapses the whole sub-agent run into one atomic tool call from
root_agent's perspective. So the granular per-tool-call trace and
timestamps quoted in the task description (`[search_email_ids]`,
`[get_emails_bulk]`, `14:55:05 Sending out request` / `14:58:58 Response
received`) are **not present in either JSON file** — they must have come
from stdout/console output captured live during the run, external to these
two files. I treat that console trace as given evidence (the task states it
was observed directly), but I flag anywhere I'm relying on it rather than
on something I could independently re-derive from the JSON.

What **is** independently verifiable from the JSON files is the wall-clock
gap between root_agent's `function_call` and `function_response` events
(`session_details.events[i].timestamp`, unix epoch seconds):

- preview: call at `1786218897.373951` → response at `1786219138.543351` → **≈241.2s** total for the entire `tracker_agent` sub-invocation.
- staging: call at `1786218590.289749` → response at `1786218834.027492` → **≈243.7s** total for the entire `tracker_agent` sub-invocation.

These totals are nearly identical, which at first looks like it contradicts
the "3m53s single call vs 65s" claim — see A3 for why it doesn't.

---

## A1 — Do INTENT 1 and INTENT 2 instructions differ in what they direct after `get_emails_bulk`?

Yes, and both direct a tool call — neither path is silent about what to call next.

INTENT 1 (preview), `agents/tracker_agent.py:19-39`:
> "If the user asks what you would write, or to show/preview entries without
> writing, search and extract entries as usual, **then call preview_resolve
> with them** and present ITS output" (`agents/tracker_agent.py:20-23`)

> "INTENT 1 prohibits stage_write and commit_write ONLY. **Calling
> preview_resolve is required, not optional** — it is the only way to report
> correct dates." (`agents/tracker_agent.py:31-32`)

INTENT 2 (search-and-stage), `agents/tracker_agent.py:40-55`:
> "If the user asks to update, sync, or add to the tracker, search emails,
> extract entries, **call stage_write with them**, then report the diff it
> returns" (`agents/tracker_agent.py:41-43`)

> "For THIS intent only, **you MUST call stage_write**. It is the only way to
> determine what would change" (`agents/tracker_agent.py:51-52`)

**Conclusion: no instruction defect here.** The preview path is directed to
call `preview_resolve` with language at least as forceful ("required, not
optional") as the staging path's directive to call `stage_write` ("you
MUST"). Option A6(a) — "INTENT 1 path never directs the resolve call" — is
**not supported** by the instruction text.

## A2 — Was `preview_resolve` called? Was `find_application_date` called? Did it stop because it decided to, or because something errored?

From the JSON alone: I cannot see inside `tracker_agent`'s own trajectory
(see scope note above), so I cannot directly confirm or refute a
`preview_resolve` call from these files. What I *can* confirm directly from
the JSON:

- `tracker_agent`'s function_response to root_agent was a literal empty
  string: `{"result": ""}` (`eval_agent_tracker_preview_....json`,
  `session_details.events[2]`, `function_response.response == {"result": ""}`).
- Contrast with staging, whose function_response was a fully populated diff
  narrative ~2500 characters long, ending with an explicit
  "Duplicates in batch" / "Unseen rows" / "Invalid entries" report
  (`eval_agent_tracker_staging_....json`, `session_details.events[2]`).
- `run_log.jsonl` — written only by `stage_write` (`tools/write_to_sheet.py:648,664,739`),
  never by `preview_resolve` (confirmed by reading `preview_resolve`,
  `tools/write_to_sheet.py:569-613` — it has no `_log_run` call anywhere in
  its body) — has exactly one entry for 2026-08-08
  (`ts: 2026-08-08T19:53:48...`, `ids_searched: 51, ids_fetched: 51`), which
  matches the staging case, not a second stage_write-adjacent event for
  preview. This rules out `stage_write` having been called during the
  preview invocation, but it says nothing either way about `preview_resolve`,
  since that function never writes to this log by design.

Per the task-provided console trace (external to the JSON, per the scope
note): "no further tool calls at all" after `get_emails_bulk`. Taking that
at face value, `preview_resolve` was **not called**.

Whether the agent "decided" to stop or "errored": there is no `error_code`
or `error_message` set on any event in the preview JSON
(`session_details.events[*].error_code` is `null` throughout, including on
root_agent's own two model calls, which both show `"finish_reason": "STOP"`
— `eval_agent_tracker_preview_....json` lines ~393-398, ~475-497). Root_agent
itself did not error. But root_agent's own finish_reason tells us nothing
about what happened *inside* `tracker_agent`'s sub-invocation, since (per
the scope note) that model call isn't logged as a separate event here at
all. **I cannot independently confirm from the JSON whether `tracker_agent`'s
internal model call raised an exception, hit a token/output limit, or
completed "successfully" with empty content** — the framework only exposes
to root_agent an opaque `{"result": ""}`, which is consistent with any of
those. This is the one point in the investigation where the available
evidence is genuinely insufficient to distinguish "errored" from "decided
to stop, badly" — flagged as SPECULATION-adjacent below in A6.

## A3 — What was in the long single call? Context size comparison.

**Estimate (labelled speculation on the exact number, not on the shape of
the payload):** `get_emails_bulk` returns every fetched email as a dict with
`id`, `from`, `to`, `subject`, `date`, `body`, where `body` is capped at
`_MAX_BODY_LENGTH = 2000` characters (`tools/get_email_detail.py:7`,
enforced at `tools/get_email_detail.py:74-75`). Both cases fetched 51 emails
of the same query (per the task description). 51 emails × up to 2000 chars
of body alone ≈ **up to ~102,000 characters** of tool-result text injected
into `tracker_agent`'s next model turn, before headers/JSON structure
overhead. This is an order-of-magnitude estimate, not something I measured
directly (SPECULATION on the precise figure — I have no `prompt_token_count`
for `tracker_agent`'s own call, only root_agent's, which is a completely
separate, small call: 1179–1223 tokens, `eval_agent_tracker_preview_....json`
lines ~393-497).

**Does staging's equivalent call carry the same payload?** Yes — both cases
run the identical `search_email_ids` query and both call `get_emails_bulk`
over the full result set (51 ids each, per the task description), so the
raw tool-result payload fed into the next model turn should be essentially
the same size on both paths. The difference is what happens *after* that
point:

- Staging: one large extraction call, then — per `tools/write_to_sheet.py`'s
  `_resolve_dates` (`tools/write_to_sheet.py:226-284`) — a **separate**
  `find_application_date` call for every entry lacking an observed
  confirmation email (`tools/write_to_sheet.py:261-267`). `find_application_date`
  itself makes its own Gmail API calls and its own separate
  `genai.Client(...)` call (`tools/find_application_date.py:346`,
  `tools/find_application_date.py:594,607`) — i.e. staging's total wall time
  is spread across **one big call plus up to ~36 small, separate calls**,
  each with a small individual context.
- Preview: `preview_resolve` runs the exact same `_resolve_dates` step
  internally (`tools/write_to_sheet.py:606`, sharing the same function as
  `stage_write` at `tools/write_to_sheet.py:697`) — meaning if
  `preview_resolve` had been reached and had run to completion, the total
  wall-clock shape should look similar to staging's (one big call + many
  small ones). Per the task's console trace, that never happened here:
  there was exactly **one** model call, and it alone took 3m53s (vs 65s for
  staging's *first* call) with nothing after it.

This reconciles the seemingly contradictory finding from the top of this
document: root_agent-visible total time for the whole `tracker_agent`
sub-invocation was nearly identical on both paths (≈241s preview vs ≈244s
staging, independently measured from JSON timestamps). Staging's ≈244s is
made up of many calls; preview's ≈241s is (per the console trace) almost
entirely **one single call** that produced nothing. That single call being
~3.5x slower than staging's structurally-identical first call, over what
should be a same-sized payload, and terminating with zero visible output
(no tool call, no text — `{"result": ""}` at the root level) is consistent
with the model exhausting its response budget on internal reasoning
(`thoughts_token_count` was non-trivial even on root_agent's own small
calls — 70 and 482 tokens on ~1.2K-token prompts, `eval_agent_tracker_preview_....json`
lines 397, 674 — suggesting this model/config combination reasons at
length even on simple decisions; over a ~100K-character context, the same
tendency plausibly consumes the entire output budget before any visible
content is emitted). **This causal mechanism is SPECULATION** — I cannot
see `tracker_agent`'s own `finish_reason` or `usage_metadata` for that call
in either file, only infer it from timing and the empty result.

## A4 — Did root_agent drop "but don't write anything"? Is it instructed to paraphrase into INTENT labels?

**Confirmed, and this is a second, separate, more concretely-provable
defect from A2/A3.**

`agent.py`'s routing instructions for preview requests, `agent.py:66-69`:
> "If the user asks to preview, show, or see what would be added WITHOUT
> writing, **that is INTENT 1**. Pass the request to tracker_agent as a
> preview and never describe it as staging. Do not paraphrase a preview
> request into a stage request."

Note what this text does and doesn't say: it tells root_agent to recognize
the request as "INTENT 1" (a label that otherwise only exists in
`tracker_agent`'s *own*, internal instruction vocabulary —
`agents/tracker_agent.py:19` etc. — root_agent has no business emitting that
label itself). It explicitly forbids paraphrasing into a **stage** request,
but says nothing about preserving the user's original wording in general,
and nothing that would stop root_agent from paraphrasing into a **shortened
preview-labeled** request.

The actual delegated call, `eval_agent_tracker_preview_....json`,
`session_details.events[1].content.parts[0].function_call`:
```json
{"request": "INTENT 1: show additions for june and july 2026"}
```
against the original user text:
```
"show me what you would add to my job application tracker from june and
july 2026, but don't write anything"
```
— root_agent invented an "INTENT 1: ..." prefix (a label it was never told
to attach to the *delegated message*, only to use for its own internal
routing decision) and, in condensing the request to fit that label,
silently dropped "but don't write anything" entirely.

**This is directly falsifiable against the staging case**, which took the
same round-trip path through the identical instruction block structure but
via the non-INTENT-1 branch (`agent.py:46-48`, "pass the user's request
directly to tracker_agent" — no INTENT label mentioned at all for this
branch). Staging's actual delegated call,
`eval_agent_tracker_staging_....json`, `session_details.events[1]`:
```json
{"request": "update my job application tracker with application emails from june and july 2026"}
```
— which is **character-for-character identical** to the user's original
text (`session_details.events[0]`). Root_agent passes the request verbatim
when nothing in its instructions primes it to relabel the message; it
paraphrases and drops a clause specifically on the one branch whose
instructions introduce an "INTENT 1" label into the model's context.

This is also independently confirmed by the eval framework itself:
`evals/tracker_preview.test.json`'s expected trajectory
(`intermediate_data.tool_uses[0].args`) is the user's **verbatim** request
text, and `tool_trajectory_avg_score` scored **0.0** for this run
(`eval_agent_tracker_preview_....json`, `overall_eval_metric_results[0]`,
`metric_name: tool_trajectory_avg_score, score: 0.0`) — an exact-match
failure against that expected verbatim string, which is a second,
independently-scored symptom of the same root cause. The eval file's own
description field additionally documents a near-identical prior failure:
*"run 3 passed a paraphrased request ('INTENT 1: stage new entries from
June and July 2026' — root_agent's own paraphrase, not verbatim) to
tracker_agent"* (`evals/tracker_preview.test.json`, `description` field) —
this is a recurring pattern, not a one-off.

**Does tracker_agent depend on receiving the original wording, or is the
INTENT 1 label alone sufficient?** `tracker_agent`'s own instructions
classify intent from free-text content ("If the user asks what you would
write, or to show/preview entries without writing" —
`agents/tracker_agent.py:20-21`), not from a caller-supplied "INTENT N:"
protocol tag; there is no text anywhere in `agents/tracker_agent.py`
that says it trusts or looks for a leading "INTENT 1:" label from its
caller. That the actual delegated text still contained the word "show" and
literally the string "INTENT 1" plausibly kept `tracker_agent` on the
preview branch here (consistent with A2's finding that `stage_write` was
never called, per `run_log.jsonl`), but this is not guaranteed by
`tracker_agent`'s instructions and is not something the current design can
rely on. Regardless of classification outcome, the dropped clause is a real
information-loss defect on its own: even if `tracker_agent` correctly
inferred "preview" from context, it never received the user's explicit
"don't write anything" constraint as backup context.

## A5 — Does the preview path depend on state the staging path sets up? Cross-invocation state?

**No evidence of state contamination; the "0 cached" observation is
consistent with a clean run, not with contamination.**

- `preview_resolve` reads `get_last_search_count()` and `get_fetched_ids()`
  (`tools/write_to_sheet.py:591-592`) to run the same fetch-completeness
  guard `stage_write` runs (`tools/write_to_sheet.py:593-595` vs
  `tools/write_to_sheet.py:646-660`). Neither reads anything `stage_write`
  *writes* (`pending_write.json`, or the sheet itself) — `preview_resolve`
  never touches `_PENDING_WRITE_PATH` at all (confirmed by reading
  `tools/write_to_sheet.py:569-613` in full; only `stage_write`,
  `tools/write_to_sheet.py:730`, and `commit_write`,
  `tools/write_to_sheet.py:778-802`, reference that path).
- `get_email_detail`'s module-level `_FETCHED_IDS` / `_BODY_CACHE`
  (`tools/get_email_detail.py:14-15`) are reset at the start of **every**
  `search_email_ids` call — `reset_fetched_ids()` is called unconditionally
  at `tools/search_email_ids.py:45`, before either case's search runs. Since
  both the preview and staging cases each issued their own
  `search_email_ids` call (per the task description), each invocation's
  cache/fetch-set starts empty regardless of what a prior case did.
- The task notes the preview run showed "0 cached" on its `get_emails_bulk`
  call. Given the reset behavior above, a cold cache (0 cached) is exactly
  what a correctly-reset invocation should show — it does not imply
  contamination happened elsewhere; if anything, a *nonzero* cached count
  reusing another case's ids would have been the anomalous signal, and that
  was not observed.
- `get_last_search_count()`/`get_last_search_range()` are also globals,
  reset on every `search_email_ids` call (`tools/search_email_ids.py:39-43,71-72`),
  so the same reasoning applies — no stale range or count could have
  leaked from the staging run into the preview run, since preview issued
  its own fresh search first.

**Conclusion: A6(cross-state) is not supported.** Nothing in the preview
path depends on `stage_write`-produced state, and the module-level caches
are reset per-search, not per-process, so there's no plausible mechanism
for staging's run (which happened earlier in the same eval session) to have
affected preview's.

## A6 — Which cause(s) does the evidence support?

Two distinct, independently-caused defects, not one:

**Defect 1 — the empty/zero-result final response.** Best-supported cause:
**(b) context-size / model-output degradation**, with caveats:
- (a) instruction defect is **ruled out** — A1 shows the preview path's
  directive to call `preview_resolve` is explicit and mandatory, not
  ambiguous or missing.
- state contamination is **ruled out** — A5.
- (b) is supported by: the empty `{"result": ""}` returned by `tracker_agent`
  as a whole (hard evidence, A2); the ~3.5x longer single-model-call latency
  reported for preview vs. staging's structurally identical first call,
  over what should be a same-sized ~100K-character payload (A3, partly
  external console evidence); and the fact that staging's equivalent call,
  under the same payload, *did* successfully produce a tool call.
- **What would settle this conclusively:** none of the current logging
  captures `tracker_agent`'s own model call's `finish_reason` or
  `usage_metadata` (the scope note explains why — `AgentTool` hides
  sub-agent events from the parent's recorded trajectory). Confirming (b)
  would need either (i) re-running with ADK's session logging pointed at
  `tracker_agent` directly (not through root_agent), or (ii) an experiment
  varying `_MAX_BODY_LENGTH` or the email count for a preview-only request
  and observing whether the empty-response failure tracks payload size.
  Without that, (b) is the best-supported hypothesis but not fully proven —
  labelled accordingly, and **no pipeline restructuring is applied in Phase
  B for this one**, per the task's rules for a context-size cause.

**Defect 2 — the dropped "but don't write anything" clause /
`tool_trajectory_avg_score` failure.** This one **is** conclusively
supported: **an instruction-priming defect in `agent.py`**. The "that is
INTENT 1" phrasing at `agent.py:67` primes root_agent to relabel its
delegated message with an "INTENT 1:" prefix it invents on the spot,
during which it drops constraint clauses — directly falsified against the
staging branch, which has no such label in its instructions and passes the
user's text through character-for-character unchanged (A4). This is not
one of the four preset options in the task (closest is (c), but I could not
independently confirm this *changed tracker_agent's intent classification*
— only that it damaged trajectory fidelity and dropped a constraint,
matching (c) in mechanism but not fully in the stated consequence). Filed
as a distinct, confirmed instruction defect, fixed in Phase B below since
the task's instruction-edit exception applies to any confirmed cause in
`agent.py` or `agents/tracker_agent.py`, not only ones matching option (a)
verbatim.

These two defects are independent: Defect 2 concerns the ~15-word user
request text (negligible size either way) and cannot itself explain a
~100K-character-context call taking 3.5x longer than normal; Defect 1
concerns what happens deep inside `tracker_agent` after email content is
already loaded, regardless of how the request was worded.

---

## Phase B — Fix

Per the task's rules, only the conclusively-supported cause (Defect 2, the
`agent.py` instruction-priming defect) is fixed with an instruction edit.
Defect 1 (context-size) is **not fixed** — no pipeline restructuring is
applied without confirmation, per the task's explicit rule for this cause.
See "Recommendations for Defect 1" below instead.

**Change**, `agent.py`, the INTENT 1 / preview bullet (`agent.py:66-69`):

Before:
```
"- If the user asks to preview, show, or see what would be added WITHOUT "
"writing, that is INTENT 1. Pass the request to tracker_agent as a preview and "
"never describe it as staging. Do not paraphrase a preview request into a stage "
"request.\n\n"
```

After:
```
"- If the user asks to preview, show, or see what would be added WITHOUT "
"writing, pass their request to tracker_agent VERBATIM — do not summarize, "
"relabel, or drop any part of it, especially any clause saying not to write "
"or stage anything. Never describe it as staging, and never paraphrase a "
"preview request into a stage request.\n\n"
```

Rationale: removes the self-invented "that is INTENT 1" label (the
demonstrated source of the paraphrase-and-drop behavior, per A4) and
replaces the implicit "pass the request" with an explicit verbatim
requirement that directly protects the exact clause that was lost
("especially any clause saying not to write or stage anything"). Minimal,
targeted at the confirmed mechanism; does not touch the staging/commit
branches, which already behave correctly without any such label.

### Recommendations for Defect 1 (context-size) — not implemented, for confirmation

Options, with tradeoffs, none implemented:

1. **Lower `_MAX_BODY_LENGTH` specifically for the preview path.** Would
   shrink the ~100K-character payload proportionally. Tradeoff: preview
   would then see materially less of each email body than staging does,
   risking preview reporting different (or missing) entries/dates than a
   subsequent stage would actually produce — directly at odds with the
   existing instruction "A preview must report the same date it would
   stage" (`agents/tracker_agent.py:94-95`). Would need its own eval
   coverage to confirm it doesn't just trade one failure mode for another.
2. **Preview-specific batch limit on `get_emails_bulk` / chunked
   preview_resolve calls.** Process emails in smaller batches, presenting a
   preview incrementally or aggregating partial results. Tradeoff:
   real architecture change to a supposedly read-only, side-effect-free
   preview path; adds statefulness (partial batches) to a codepath whose
   only current job is "answer once, no side effects"; higher engineering
   risk than a single instruction edit.
3. **Cap the number of emails a preview will process, and say so.** e.g.
   preview refuses (à la the existing fetch-completeness refusal in
   `preview_resolve`, `tools/write_to_sheet.py:593-595`) above some N,
   telling the user to narrow the date range. Tradeoff: simple and safe,
   but degrades preview's usefulness for exactly the large date ranges
   where a preview is most valuable, and doesn't fix the underlying
   capacity limit — just fails more gracefully at it.
4. **Do nothing structural; instead make root_agent's "never return an
   empty response" fallback distinguishable from a real zero-results
   answer** — e.g. `tracker_agent` could return a distinct sentinel/error
   string instead of empty text on generation failure, so root_agent's
   summary can honestly say "the preview failed to complete" instead of
   fabricating "I didn't find any updates." This doesn't fix the underlying
   model behavior but stops it from being reported to the user as a false
   negative. Smallest blast radius of the four, and directly targets the
   most user-visible harm (silent wrong answer) without guessing at the
   model-level cause.

Also worth noting: `evals/tracker/test_config.json` includes the
`hallucinations_v1` metric (`evals/tracker/test_config.json:3-6`), but
`evals/test_config.json` — used for `tracker_preview.test.json` — does
**not** (`evals/test_config.json:1-4`). Root_agent's fabricated "I didn't
find any job application updates..." summary, synthesized from an empty
tool response, is exactly the kind of hallucination that metric exists to
catch, but it is never run against the preview case. This is an eval
coverage gap, not something changed here (touching eval configs is out of
scope per the task's constraints), but worth flagging for a future
decision.

---

## Phase C — Regression coverage

**Defect 2 (agent.py instruction fix): partially testable, added.** The
actual failure mode (an LLM choosing to paraphrase) cannot be unit tested
without live model calls. What *is* testable and was regressed here is a
static property of the instruction text: it must no longer contain the
"INTENT 1" self-priming label that A4 identified as the mechanism, and it
must explicitly require verbatim forwarding, including protection for a
"don't write/stage" clause. A test asserting this text-level invariant
would have failed against the pre-fix instruction string and passes
against the fixed one — added to `tests/test_agent_instructions.py`.

**Defect 1 (context-size / empty response): not unit-testable, and no test
is added for it.** Stated explicitly per the task's instructions rather
than writing a test that only appears to cover it: this defect is a live
Gemini model's behavior under a large context (whether it emits visible
output before exhausting its response budget). There is no deterministic
code path to assert against — `tracker_agent`'s model call, its context
size, and its output are all runtime, live-API-dependent behavior, not
something `tools/write_to_sheet.py` or `agents/tracker_agent.py` control
directly. A test that mocks the model to return empty text and then asserts
"the caller handles it" would only be testing the mock, not the actual
defect (whether the model produces empty text in the first place), which
was explicitly the failure mode the task asked me not to fake coverage
for. If option 4 from the Defect-1 recommendations above (a distinguishable
failure sentinel) is implemented later, *that* would introduce a real,
testable code path — but it isn't implemented in this pass.

---

# Investigation: hallucinations_v1 0.5 on tracker_staging (2026-08-09, ~16:52 run)

Read-only investigation — no code changes, no eval runs. Answers Q1–Q4 from
the task with file:line/JSON-path evidence for every claim; speculation is
labelled explicitly as SPECULATION. Recommendations only — nothing below is
implemented.

Primary sources read in full:
- `eval_agent/.adk/eval_history/eval_agent_tracker_staging_1786312368.767647.evalset_result.json`
  (the run in question — `creation_timestamp` converts to 2026-08-09
  16:52:48 local time, matching the task's "~16:xx")
- The four earlier same-day `tracker_staging` runs, for comparison:
  `..._1786303217.887519...json` (14:20), `..._1786304359.034569...json`
  (14:39), `..._1786305424.2007182...json` (14:57),
  `..._1786308029.36231...json` (15:40) — all four scored
  `hallucinations_v1 = 1.0` on the same eval case
- `evals/tracker/tracker_staging.test.json`
- `tools/write_to_sheet.py` (`stage_write`, `_merge_duplicates`, `_resolve_dates`)
- `tools/get_email_detail.py` (`_BODY_CACHE`, `reset_fetched_ids`)
- `tools/search_email_ids.py` (the `reset_fetched_ids()` call site)

**Same scope limitation as the 2026-08-08 investigation above applies
here too:** the eval history JSON only records **root_agent's own
trajectory**. There are exactly 4 top-level events in this case
(`eval_metric_result_per_invocation[0].actual_invocation.intermediate_data.invocation_events`,
lines ~660–1053) — two `tracker_agent` `function_call`/`function_response`
pairs plus root_agent's own intermediate and final text. `tracker_agent`'s
own internal steps (its `search_email_ids`, `get_emails_bulk`, and both
actual `stage_write` calls the task's stdout evidence already established)
run *inside* those two root-level calls and are not separately logged here.

## Q1 — Per-segment hallucinations_v1 results: how many segments, which flagged, verbatim reasoning?

**Cannot be answered as asked — the per-segment breakdown is not present
anywhere in this file.** This is itself the first finding.

- The metric's own result is `{"metric_name": "hallucinations_v1", "score":
  0.5, "eval_status": 2, "details": {"rubric_scores": []}}` — present
  twice, identically: in `overall_eval_metric_results[1]` (file lines
  ~70–82) and again in `eval_metric_result_per_invocation[0].eval_metric_results[1]`
  (lines ~497–509). Both have a **bare empty list** for `rubric_scores`.
- Contrast with the sibling metric in the *same case*,
  `rubric_based_final_response_quality_v1`: its `details.rubric_scores`
  (lines ~8–50) is fully populated — 3 entries, each with a `rubric_id`, a
  multi-sentence `rationale`, and a `score`. The logging plumbing for
  per-item judge reasoning clearly exists and works in this file; it's just
  never populated for `hallucinations_v1`.
- The criterion block confirms `evaluate_intermediate_nl_responses: true`
  was actually in effect for this run (line ~75), matching the task's
  premise. Turning that flag on makes the judge *score* intermediate
  segments; it does not make this ADK build persist *why* to eval history.

**So there is no verbatim judge reasoning to quote, for any segment.**
What can be reconstructed instead, from the raw trajectory plus the score
arithmetic (**SPECULATION on the mapping from segments to the 0.5 value** —
not read from any judge output):

The trajectory contains exactly two candidate NL segments for
`evaluate_intermediate_nl_responses` to score:
1. The one intermediate text, `"The staging step did not run. I will ask
   the tracker agent to stage the write again."` (line ~797, see Q2).
2. The final response (`actual_invocation.final_response`, matches line
   ~1001).

The task's own already-established evidence says the final response is
grounded (its counts match the second `[stage_write]` stdout line exactly).
If `hallucinations_v1` weights segments equally, 0.5 over 2 segments with
one already confirmed grounded is arithmetically consistent with **exactly
the intermediate segment being the one flagged unsupported**, and no other
combination fits both the score and the already-confirmed-grounded final
response. This is an inference from arithmetic and elimination, not a
transcript of judge output — flagged as SPECULATION accordingly.

## Q2 — Why did stage_write run twice? Who triggered the second run, and what did the intermediate NL response say?

**Directly answered from the JSON — no speculation needed here.**

All 4 top-level events are authored by `root_agent`:

- Event 0: `root_agent` → `tracker_agent`, `{"request": "update my job
  application tracker with application emails from june and july 2026"}`
  (user's text, verbatim).
- Event 1: `tracker_agent`'s response (line ~717): *"I found 37 new
  applications and no status changes. There were no duplicates in this
  batch, no existing tracker entries that this run did not find and
  therefore did not verify, and no invalid entries."* — note this
  response's counts (37 new, 0 duplicates) match the task's **first**
  `[stage_write]` stdout line ("0 duplicates in batch") exactly, confirming
  `stage_write` genuinely ran during this first `tracker_agent` call, even
  though the response prose never uses the word "staged."
- Event 2: `root_agent` emits visible text **before** its next call (line
  ~797): *"The staging step did not run. I will ask the tracker agent to
  stage the write again."* — then calls `tracker_agent` a second time,
  `{"request": "stage the write for application emails from june and july
  2026"}`.
- Event 3: `tracker_agent`'s second response (line ~921) — the full
  itemized diff (37 new / 0 status / 1 duplicate, CrossLink Professional
  Tax Solutions, 2026-05-31) that the final response echoes verbatim.

**The second run was triggered by `root_agent` itself, not by any
`tracker_agent`-internal retry.** The intermediate NL message between the
two runs is exactly: *"The staging step did not run. I will ask the
tracker agent to stage the write again."*

This text is a close paraphrase of a literal bullet in `root_agent`'s own
system instructions, recorded in this same file at
`app_details.agent_details.root_agent.instructions` (line ~287):

> "Only tell the user the tracker is up to date if tracker_agent returned
> an explicit staged diff with has_changes false. If tracker_agent claims
> the tracker is up to date without reporting staged counts, do not repeat
> that claim. Tell the user the staging step did not run and ask
> tracker_agent to stage the write again."

So the re-delegation is root_agent correctly following an instructed
fallback branch — but the branch's trigger condition ("claims the tracker
is up to date *without reporting staged counts*") doesn't cleanly describe
what event 1 actually contains: tracker_agent's first response **did**
report staged counts (37 new, 0 status changes, 0 duplicates) and never
claimed the tracker was "up to date." Root_agent treated the response as
ambiguous/insufficiently explicit anyway. This mismatch is the crux of Q4.

## Q3 — Why did the duplicate count differ (0 then 1) given identical 37-new counts?

**Cache/refetch mechanics are ruled out by code; the likely cause is
LLM entry-extraction non-determinism across the two separate `tracker_agent`
invocations — labelled SPECULATION, since the actual `entries` argument
passed to either `stage_write` call is not present in this JSON.**

Ruling out `_BODY_CACHE` / `reset_fetched_ids`:
- `reset_fetched_ids()` clears both `_FETCHED_IDS` and `_BODY_CACHE`
  (`tools/get_email_detail.py:23-26`) and runs unconditionally at the top
  of every `search_email_ids` call (`tools/search_email_ids.py:45`). If
  `tracker_agent` re-ran its own search for the second "stage the write..."
  request (plausible — it is a fresh top-level delegation, not something
  root_agent tells it retains state from the first call), that reset gives
  a **clean, empty** cache going into the second pass, not a stale one that
  could carry over a leftover id from the first pass.
- The underlying Gmail data is read-only and did not change in the seconds
  between the two calls, so a second `search_email_ids` should return the
  same message-id set, and `get_email_detail`/`get_emails_bulk` should
  return identical body content per id whether served from cache or
  refetched. Nothing in this code path can synthesize an *extra distinct
  entry* purely from a cache reset.

What actually decides `duplicates_in_batch`
(`tools/write_to_sheet.py:702`, `_merge_duplicates` at lines 287-330): it
groups the `entries` list `stage_write` is called with by normalized
`(company, role)`, then unions groups whose company keys are
prefix-related (lines 292-300) — the function's own docstring uses, as its
illustrative example, a short company form merging with its full form,
naming **"CrossLink"** merging with **"CrossLink Professional Tax
Solutions"** (line ~296) — the exact company in this case's flagged
duplicate. Anything beyond the first entry in a merged group counts as a
duplicate-in-batch. The `entries` list itself is built by `tracker_agent`'s
LLM extraction step *before* `stage_write` is ever called, and that
extraction step is not logged in this JSON (same `AgentTool` opacity noted
in Q2/the scope note).

Given that: the identical final new-row count (37, both times) shows the
post-merge deduped set was the same either way — only the *duplicates-in-batch
accounting* differed. The explanation that best fits (a) the identical
37-new outcome, (b) the differing duplicate count, and (c) how
`_merge_duplicates` actually works, is that on the second pass
`tracker_agent`'s extraction step emitted **two** raw entries for the
CrossLink Professional Tax Solutions / Software Engineer I application
(plausibly one from an application-confirmation email and one from the
2026-05-31 rejection email, under two slightly different company-name
strings that the prefix-match rule then merges), where on the first pass
it emitted only one already-consolidated entry for the same underlying
emails. **This is SPECULATION on the precise mechanism** — I cannot see
either call's actual `entries` argument — but it is consistent with all
observed evidence, and the cache/refetch alternative is ruled out by the
code, not merely unlikely.

## Q4 — Verdict

**Best supported: (a) — a genuine, correctly-flagged unsupported (in fact
contradicted) intermediate claim — with (c) as the mechanical trigger for
*why a second run existed to talk about at all*, not as an independent
artifact. (b) judge-variance-on-borderline-segments is the weakest fit.**

Reasoning:
- Across the 4 other `tracker_staging` runs earlier the same day, **none
  re-delegated** — each is a single 2-event exchange (one `tracker_agent`
  call, one response), scoring `hallucinations_v1 = 1.0` every time. Several
  of those first-call responses **also** omitted explicit "staged" language
  (e.g. the 14:20 run's response: *"I found 36 new applications and no
  status changes. There were no unchanged entries."* — no mention of
  staging at all), yet root_agent did not invoke its fallback branch in
  those cases. So root_agent's decision to fire "the staging step did not
  run" is **non-deterministic** given near-identical first-call phrasing —
  this run is the 1-of-5 case where it fired. That rules out a fixed
  pipeline rule forcing two runs; it's a probabilistic root_agent judgment
  call, per-run.
- Whether that judgment call, once made, is *true*: the task's own
  established stdout evidence shows the first `[stage_write]` line already
  logged "0 duplicates in batch" with the same 37-new count that
  tracker_agent's first response reported — i.e. staging **did** run on
  the first call. Root_agent's claim "The staging step did not run" is
  therefore not merely unsupported by visible evidence — it's
  **contradicted** by evidence already in the same trajectory. That is
  exactly the failure mode `hallucinations_v1` exists to catch, which
  argues for (a) over (b) or (c) as a distinct root cause: the
  double-staging is *downstream of* this false claim, not a separate
  artifact that corrupted an otherwise-clean trajectory.
- (b) can't be fully ruled out for the precise 0.5 arithmetic — no
  per-segment reasoning is logged (Q1) to confirm which segment was
  flagged or why — but the 5-run comparison makes pure judge noise a
  weaker explanation than "this run genuinely contains a false claim the
  other 4 don't."

**Where a fix would belong, if pursued (nothing implemented):**
- **Instructions (`agent.py`, root_agent):** the fallback bullet ("If
  tracker_agent claims the tracker is up to date without reporting staged
  counts...") is firing on responses that **do** report staged counts (37
  new, 0 status changes, 0 duplicates) — its trigger condition is
  under-specified relative to what tracker_agent's responses look like in
  practice. Tightening it (e.g., treat "found N new applications" as
  evidence staging occurred, regardless of whether the word "staged"
  appears) would remove the root cause without any pipeline change. This
  matches the pattern already used for the confirmed root_agent-side
  defect in the 2026-08-08 investigation above (instruction edit preferred
  over restructuring where the defect is root_agent's own prose).
- **Pipeline:** no evidence supports a pipeline-level cause for the *second
  run existing* — there's no code path that forces two `stage_write` calls;
  it is entirely root_agent's own probabilistic interpretation of prose. A
  pipeline change (e.g., `tracker_agent` always returning a structured
  `has_changes`/`staged` flag rather than leaving root_agent to infer it
  from free text) would remove the ambiguity at the source rather than
  asking root_agent to parse prose more carefully, and is arguably more
  robust — but it's a larger change than an instruction edit and isn't
  required to fix this specific defect.
- **Eval config (`evaluate_intermediate_nl_responses` back to `False`):**
  would hide this finding, not fix it. Flagging explicitly per the task's
  ask: this flag is exactly what caught a real inconsistency between what
  root_agent said and what its own tool evidence already showed (Q2);
  turning it off would make `hallucinations_v1` check only the final
  response, silently losing coverage on this entire class of intermediate
  false claim. Not recommended as the primary fix — at most a stopgap
  while an instruction fix is pending, with that coverage loss stated
  up front rather than discovered later.

**Separately worth flagging:** the empty `rubric_scores` for
`hallucinations_v1` (Q1) is a standalone observability gap, independent of
this case's outcome. Any future sub-threshold `hallucinations_v1` score
will require the same reconstruct-from-raw-trajectory-and-arithmetic
approach used here rather than reading judge reasoning directly, unless
that logging gap is closed.
