# Engineering Log

A record of every significant bug in this project: the symptom, the theories that
turned out to be wrong, the actual cause, and the fix. Written to be re-read.

The single most useful thing here is the pattern of **wrong theories**. Most of
them were reasonable, and every one was killed by evidence rather than by
argument.

---

## The presenting bug: a rejection recorded as "Applied"

**Symptom.** Cisco's only email in the requested range was a rejection. The
tracker wrote the row as `Applied`.

### Three theories, all wrong

**Theory 1 — the fix never reached the running code.** Spec-first development's
classic failure: `agent_spec.yaml` updated, the instruction string in
`tracker_agent.py` never regenerated from it.
*Killed by:* `grep` found the rule present in the code. (It turned out to be
missing from the *spec* — the opposite direction. A real problem, but not this one.)

**Theory 2 — grouping split the company into two rows**, one `Applied` from a
confirmation, one `Rejected` from the rejection.
*Killed by:* there was exactly one row.

**Theory 3 — the agent was classifying from search previews, not full bodies.**
*Killed at the time by:* 48 `get_email_detail` calls in the log.
*Note:* this theory was correct, just premature — it resurfaced later as a real
failure. See "The snippet shortcut" below.

### First real cause: the keyword checklist

The instruction mapped literal phrases to statuses:

```
'thank you for applying', 'received your application'  = Applied
'regret', 'not moving forward', 'other candidates'     = Rejected
```

Cisco's email opens with *"Thank you for applying to the Software Engineer I..."* —
an exact match on `Applied`. Its rejection is worded *"unable to move you forward"*
and *"large pool of candidates"* — matching **none** of the `Rejected` phrases.

So it wasn't first-match-wins. The `Rejected` rule never fired at all.

**Fix.** Replaced the lookup table with semantic judgment: read the body, decide
what outcome it communicates. This immediately corrected nine other companies.

**Cisco stayed wrong.**

### Actual root cause: empty bodies

A debug print showing extracted body length gave it away — Cisco extracted
**0 characters**. A standalone script dumped the MIME tree:

```
multipart/mixed
└── multipart/related
    └── text/html    13,700 chars
```

No `text/plain` part anywhere. `_extract_body` only searched for `text/plain`, so
it returned empty. The agent's entire knowledge of that email was the snippet,
which reads *"Thank you for applying."*

**Calling it Applied was the correct answer to the wrong input.**

**Roughly 20 of 48 emails were affected.** Most still landed on the right status
because their preview text happened to be accurate. Cisco was the one case where
the first 150 characters said the opposite of the email.

**Fix.** Recursive walk for `text/plain`, fall back to `text/html` at any depth,
strip tags and entities. Cisco → `Rejected`. Allstate, OCLC, Cornerstone,
GE Vernova, Doowii, CrossLink and SAIC silently corrected too — all wrong before,
none of them noticed.

**This is the story worth telling in an interview.** The visible symptom was one
wrong status. The cause was a silent data-extraction failure two layers down,
affecting 40% of inputs, and mostly masked by lucky guesses.

---

## Everything else, in order

### 2. Invented status values
Navan came back as `Hold` — not one of the four permitted statuses. The field was
unconstrained, so the model free-formed.
**Fix:** require exactly `Applied` / `Rejected` / `Interviewing` / `Offer`.

### 3. Sort order flipped between runs
Same instruction, opposite result. Sorting is deterministic work with one right
answer.
**Fix:** moved out of the agent and into `write_to_sheet`.

### 4. Context overload
Some bodies ran 7,000+ characters, and ADK resends accumulated context every step.
Caused a 429 rate-limit crash in one run and, in another, `write_to_sheet` never
firing at all while the agent claimed success.
**Fix:** truncate bodies to 2,000 characters — status is always established in the
opening paragraph.

### 5. Duplicate rows on re-run
Matching used company + role + date, but all three drift between runs:
`CrossLink Professional Tax Solutions LLC` vs. without the `LLC`,
`AXS – Charlotte, NC` vs. `AXS`, Abbott's date moving by a month.
**Fix:** a `_normalize()` comparison key — lowercase, strip parentheticals, legal
suffixes, location tails, punctuation — and dropping date from the match entirely.

**The concept worth naming: a comparison key.** Keep the original data intact for
display; derive a lossy key purely for matching. The key should discard exactly
the variation you consider meaningless and nothing more. Both sides of the
comparison must be normalized, or you've moved the mismatch rather than removed it.

### 6. Role paraphrasing — and two overcorrections
The agent summarized `Beginner Coding Assessment (NCG) – Desktop Application` down
to `Software Engineer`, which merged two distinct IBM applications into one row.
**An application vanished from the sheet.**

*Overcorrection 1:* required verbatim extraction **including requisition numbers**.
Worse — those numbers appear in some emails for an application and not others, so
single applications split into two rows and four dates collapsed onto the same day.

*Overcorrection 2:* a merge rule mentioning "location" was read as permission to
**delete** locations from role titles. `Associate Data Engineer 2026- FutureNow -
Chicago` became `Associate Data Engineer`.

**Landing spot:** keep the descriptive title, strip only ID numbers, state
explicitly that the merge rule governs *comparison between entries*, never editing
a title, and when merging keep the longer title.

### 7. Run-to-run instability
Visa produced a different status on nearly every run. Three distinct causes, only
one of which was actually randomness:

- **Underspecified rule.** An assessment invite fit neither `Applied` nor
  `Interviewing` under the existing rules. Not randomness — a gap. Fixed by
  deciding: assessments and phone screens are `Interviewing`.
- **Sampling.** Set `temperature=0`. *(Note: this was believed applied for several
  rounds before a code read revealed it had never landed. Verify, don't assume.)*
- **Wording drift.** Absorbed in code by `_normalize` rather than fought.

### 8. Feedback surveys — a correction from the user
First rule said to exclude any email containing a feedback survey. But companies
often attach a survey to a rejection or confirmation — so that rule would discard
the status along with the survey.
**Corrected to:** exclude only when the feedback request is the *entire* point of
the email. Structurally the same mistake as the original Cisco bug: classifying on
a surface feature instead of what the email communicates.

### 9. The snippet shortcut (theory 3, confirmed)
One run had **zero** `get_email_detail` calls and still produced statuses. The
agent classified straight from search-result previews, and exactly the four
formerly-empty-body companies flipped to `Applied`.

*First fix:* removed `snippet` from `search_emails` output. **Insufficient** —
results still carried Subject, From and Date, and the agent classified from those
instead. Evidence: it recorded `Entry-level Talent Recruiting` (Cisco's subject
line) as a job title and invented a June 20 date that exists in no email. Five
fabricated rows, three status regressions.

*Real fix:* a separate `search_email_ids` tool returning **IDs only**. No subject,
no sender, no date. With nothing to guess from, opening each email is the only path
to any content.

**The principle:** the instruction *said* to open every email; nothing *stopped*
the agent from skipping it. Make the shortcut impossible rather than forbidden.

### 10. The silent 10-entry truncation
Recurred across three sessions and was blamed on the agent "stopping early" each
time. A code read found:

```python
def search_email_ids(query: str, max_results: int = 10) -> dict:
```

**The default was 10.** Every time the agent omitted `max_results`, it silently
received 10 ids and dutifully processed all of them.

**Lesson:** three rounds of blaming model behaviour for a default parameter value.
Read the code before theorizing about the agent.

### 11. Fabricated success
A run read all 48 emails and then announced *"The tracker is already up to date"* —
with no `stage_write` call. It did the work and invented the conclusion.

Every validation guard lives *inside* `stage_write`, so none of them fired.

**Fix, in two layers:** `tracker_agent` is told it cannot know whether anything
changed without calling `stage_write`; and `root_agent` is told not to repeat an
"up to date" claim that didn't come with staged counts.

### 12. Dedup that destroyed the answer
The duplicate guard kept the *first* of two entries for the same application.
Entries are sorted oldest-first, so "first" meant the application confirmation and
the later **rejection was discarded** — putting Abbott and CrossLink back to
`Applied`. The exact class of bug the whole session began with.

**The code did what was specified; the specification was wrong.**

**Fix:** merge rather than drop — earliest date, latest status, longest company and
role strings. This mirrors the rule the agent itself already follows: date and
status are determined independently.

### 13. Outcome-blind reply drafting
Asked to draft a reply to the Cisco rejection, the drafting agent wrote *"I remain
very interested in this opportunity and look forward to any further information."*

`tracker_agent` held all the semantic-classification rules; `drafting_agent` had
none of them. Same root cause as the original bug, in a different agent.

**Fix:** outcome-awareness rules in `drafting_agent` — determine what the email
communicates before drafting, and never express continued interest in a role that
was declined.

### 14. Regression introduced while fixing something else
Raising `max_results` from 10 to 100 correctly fixed the tracker's truncation — and
also applied to `list_emails`, which serves user-facing inbox requests. "What emails
need my attention" became: fetch 100 emails, one metadata call each, classify all
100. Long runs and rate limiting.
**Fix:** 100 for the tracker's search only; 20 for the user-facing tools.

### 15. Two claims that were asserted and later disproven
- **"There is a consistent first-command failure."** Based on misreading one
  session's three commands as three separate sessions. There was one failure, once.
- **"The backward lookback (PASS 2) is working."** Based on a row dated before the
  requested range. The `search_emails` debug print later proved only one search ever
  runs; that row came from the primary search catching the boundary date.

---

## Transferable lessons

**Verify the input before debugging the reasoning.** Four rounds went into status
logic that was already correct. The agent was reasoning correctly over empty input.

**A *stably* wrong answer points at a rule; a *flickering* one points at missing
information.** Cisco was wrong identically every run while Figma and Hypha changed
between runs — all three had empty bodies, but Cisco's snippet was confidently
misleading and the others' were merely thin.

**Silent partial failures are the dangerous ones.** Nothing errored. 40% of emails
extracted nothing, and most still produced the right answer by luck.

**Instructions are requests; code is a guarantee.** Sorting, deduplication, and
forcing the full-body read were all fixed by removing the agent's discretion.
"Please call this tool" failed. "There is no other source of data" worked.

**Constrain the output vocabulary.** `Hold` disappeared the moment the field had a
fixed set of permitted values.

**Don't overfit to the failing example.** Quoting one company's exact sentences
into the instruction, and demanding verbatim requisition numbers, both taught the
agent about a single email at the cost of the general case.

**Guards should report zeroes, not stay silent.** `0 duplicates, 0 unseen, 0
invalid` is a result. No output is indistinguishable from a check that didn't run.

**Read the code before theorizing about the model.** The 10-entry truncation, the
missing `temperature=0`, and the un-applied HTML fix were all discovered by reading
files, after multiple rounds of behavioural speculation.

---

## Questions worth being able to answer cold

1. Why did the semantic-classification fix correct nine companies but not Cisco?
2. Why was removing `snippet` a better fix than writing a stronger instruction —
   and why wasn't removing it from `search_emails` enough?
3. Why do status and date have to be determined independently?
4. Why does `_normalize` run on the existing sheet rows as well as the new entries?
5. Why is deduplication done in code rather than by the model — and under what
   circumstances would that answer flip?
6. Why does `commit_write()` take no arguments?

---

## Later findings (evaluation phase)

These emerged after the tracker was stable, while building the eval suite. Two
are among the most serious bugs in the project's history, and both were caught
by evals rather than by manual testing.

### 16. Thirty-five fabricated job applications

During an eval run, the tracker agent searched, received 48 message IDs, opened
**none of them**, and staged 35 entirely invented applications — ExampleCorp,
GlobalTech, Quantum Labs, BioGen Pharmaceuticals — with plausible dates and
statuses. Nothing reached the sheet only because the confirmation step was
waiting for approval.

**Why removing the snippet made this possible.** Earlier, removing preview text
from search results forced full-body reads. But when the agent skips the fetch
entirely, it now has *nothing* to anchor on — so instead of misclassifying from
a snippet, it invents from scratch. The shortcut bug became a hallucination bug.

**Why the guard did not fire.** The fetch-completeness check compared
`ids_searched` to `ids_fetched` — but both were **parameters the agent passed**.
It omitted them, they defaulted to 0, and the check silently skipped itself.

**The principle: a guard that asks the component it guards for evidence is not
a guard.** Fixed by having the tools keep their own records — `search_email_ids`
records what it returned, `get_email_detail` records every ID actually fetched —
and `stage_write` compares them directly, refusing to stage if nothing was read.

**The refusal changed behaviour, not just output.** In a later run the agent
again tried to stage without reading; `stage_write` returned
`REFUSED — 48 ids searched, 0 emails read`; the agent then went back, fetched
all 48 emails, and staged correctly. The guard did not merely block bad data —
it redirected the agent onto the correct path mid-run.

### 17. The date-range guard was silently deleting SAIC

A guard added to catch fabricated dates (an entry once appeared dated April 13
from a June–July search) took `range_start` and `range_end` as parameters — from
the agent. The agent passed "June and July" per its reading of the request, but
the query it actually issued was `after:2026/05/31`, which legitimately returns
May 31 emails. Three real applications dated 2026-05-31 were rejected as
out-of-range; SAIC, whose only email was that date, vanished entirely.

Same flaw as #16 in a different spot: trusting the agent's account of what it
did instead of observing it. Fixed by parsing the date bounds out of the actual
issued query, treating both bounds as inclusive.

**Note:** this guard, not model flakiness, explains most of the intermittent
"SAIC missing" runs previously attributed to tail-of-list extraction loss.

### 18. root_agent never fetched the email it was drafting a reply to

The drafting eval caught root_agent asking inbox_agent for "most recent email
from Cisco", receiving **subject, sender, and date with no body**, then
fabricating an intent — "express continued interest in the position" — for what
was actually a rejection. The reply said exactly that.

The earlier drafting fix had hardened the wrong layer: drafting_agent was taught
to judge from the body, but root_agent never gave it one. Fixed by requiring
root_agent to explicitly demand get_email_detail output, verify body text was
actually returned, and derive intent from the body — never the subject.

### 19. Preview was staging

A preview request produced INTENT 2 vocabulary ("Would you like to write these
to the sheet?"), indicating stage_write ran — leaving an unapproved plan on disk
that a later "yes" could commit. Cause: a rule added for INTENTs 2 and 3 read
"You MUST call stage_write... You cannot know any of those things otherwise" —
phrased absolutely, contradicting INTENT 1's "Do NOT call stage_write". The
model resolved the contradiction the wrong way. Fixed by scoping the rule ("For
THIS intent only...") and having preview state plainly that nothing was staged.

**Lesson: absolute language in one rule overrides scoped language in another.**
When two instructions conflict, the model does not reliably pick the one you
meant.

### 20. What the eval tooling itself taught

- **Exact trajectory matching cannot express this system's behaviour.** Tool
  arguments embed volatile content — 48 changing message IDs, full email
  bodies, freeform request paraphrases — and ADK's matcher compares arguments
  exactly with no wildcard. Cases asserting sub-agent behaviour moved to
  LLM-judged response matching instead.
- **root_agent's trajectory shows *which sub-agents* were called, never which
  tools they used.** AgentTool calls are opaque from outside. "Exactly one
  tracker_agent call" turned out to be the *right* root-level assertion: a
  second call would mean auto-committing without confirmation.
- **The LLM judge (final_response_match_v2) once scored a buggy reply 1.0
  against a correct reference.** Better than ROUGE, not a substitute for
  reading the output on cases that matter.
- **ROUGE scored two responses making materially different claims at 0.55.**
  Word overlap cannot distinguish "staged" from "written".

### 21. Classification returned nothing, twice

"What emails need my attention" fetched 20 emails and produced no output at
all — the chain died silently after the fetch. classify_email makes one Gemini
call per email with no error handling; any single failure aborted everything.
Fixed with per-email try/except returning a safe FYI fallback, plus an explicit
never-return-empty rule. Output format also fixed: one item per line, FYI
reported as a count only — the user asked what needs attention, and FYI is by
definition what does not.

### 22. Lowering a binary metric's threshold does nothing

On the 2026-08-08 live run, `routing/routing_classification.test.json` and
`tracker/tracker_staging.test.json` both scored `0.0` on
`final_response_match_v2` while `rubric_based_final_response_quality_v1`
scored `1.0` on every rubric for the same response, the judge explicitly
confirming correct categorization. The agent was right; the reference was
wrong — each `.test.json` recorded a reference response naming specific
emails from an inbox snapshot weeks old, so a correct response about
*today's* inbox shared almost no lexical overlap with it.

**First fix, and why it did nothing.** The threshold was lowered to `0.3`,
on the theory that this was a continuous similarity score and a weak partial
match would still clear a low bar. `final_response_match_v2` is not
continuous — it's a **binary** metric: the judge outputs "valid" or
"invalid" per invocation (`google/adk/evaluation/final_response_match_v2.py:133-137`),
so with one invocation per case the score is always exactly `0.0` or `1.0`.
A stale reference produces `0.0`, and `0.0 >= 0.3` is exactly as false as
`0.0 >= 0.8`. The threshold change couldn't have fixed anything it was
tested against, because it was never exercised against the actual failure
mode.

**The real fix.** Rewrote both references to describe the *shape* of a
correct answer (priority categories, security alerts under Urgent, staged-
not-written, asks to confirm) instead of *content* tied to one inbox
snapshot — then restored the threshold to `0.8`, since that's the correct
value for a binary metric: it means "the judge must call this valid."
Rubrics (see `evals/README.md`) already carried the substantive assertions,
so tightening the reference back up cost nothing.

**Lesson: know whether a metric is continuous before tuning its threshold.**
A threshold change that isn't tested against the specific failure it's meant
to catch can look like a fix while being completely inert.

### 23. Inconsistent urgency, and an email that disappeared

Live run 2026-08-09, routing eval. Both defects were surfaced by
`rubric_based_final_response_quality_v1` (score `0.5`) while
`final_response_match_v2` scored `1.0` on the exact same response — the
similarity judge passed output the rubric judge caught as wrong. Same shape
as #22: two metrics disagreeing, and the rubric being the one worth
believing.

**Defect A — inconsistent categorisation.** Two verification codes landed in
two different categories in one response: a TikTok code (expiring in 30
minutes) under Urgent, an ADP code (expiring in 15 minutes) under Action
Needed. The judge: *"the 'ADP verification code', which is also a
time-sensitive verification code, is placed under 'Action Needed' rather
than 'Urgent'."* The judge's own framing revealed the underlying problem —
it was auditing for consistency with a *generic time-sensitivity* notion of
urgency, and the classification prompt agreed with it, because that's what
the prompt actually said. `security_alerts_under_urgent`, the routing rubric
this eval is graded against, asserted exactly that: alerts and codes belong
under Urgent. It was internally consistent with the old prompt and wrong
about what "Urgent" should mean for this system.

**The taxonomy decision.** This is a job-application assistant. The owner's
call: "Urgent" means the user's *job search* needs them now — an interview
time to confirm, an assessment with a due date, a deadline on an
application — not that some clock, anywhere, is running out. A verification
code is time-sensitive to the service that sent it, not to the job search;
the user triggered it and is already looking at their phone. Under the old
prompt this was reversed: security alerts explicitly mapped to Urgent by
name, and nothing distinguished "expires soon" from "job search needs
action." Two codes with two different expiry windows had no rule forcing
them into the same bucket, so a coin-flip level of prompt sensitivity decided
where each landed — the actual bug behind Defect A. Fixed by rewriting
`classify_email`'s prompt (governing principle + criteria + examples),
`classification_agent`'s instructions, `agent_spec.yaml`'s category
definitions, and the routing rubrics to all state the same rule: urgent is
job-search action, not generic time pressure. Verification codes and
security alerts moved from Urgent to FYI everywhere they were named.

**Defect B — a silent drop.** `inbox_agent` fetched 20 emails; the
classification summary accounted for 19 (1 Urgent + 3 Action Needed + 15
FYI). The judge: *"one email fetched by inbox_agent is not accounted for in
the classification summary."* No guard existed on this path. The equivalent
check — comparing what was fetched to what was reported — already exists for
the tracker path (`fetch_gap` in `stage_write`, #16), enforced in code
against tool-recorded counts. `classification_agent` had no analogous check,
guarded or unguarded: nothing compared the emails it was given to the
categories it produced, so one could vanish between the fetch and the
summary with no signal.

**Fix, and its limit.** Added a completeness requirement to
`classification_agent`'s instructions: every email received must land in
exactly one category, the FYI count plus the listed Urgent/Action-Needed
items must equal the total received, and the agent must say so explicitly if
it cannot account for all of them. This is an instruction-level guard only —
unlike `fetch_gap`, there is no code-level count comparison on this path, so
it is a request, not a guarantee. Per the project's own lesson (#9, #11):
"instructions are requests; code is a guarantee." The rubric
(`accounts_for_all_emails`) is what actually verifies this behaviour today;
a code-enforced count check on the classification path, mirroring
`stage_write`'s, is future work if instruction-level compliance proves
unreliable.

---

### 24. Two eval artifacts the taxonomy fix forgot

Live run 2026-08-09, after #23 landed. `rubric_based_final_response_quality_v1`
passed (`0.8333`, all taxonomy rubrics scoring `1.0`), but two failures
remained — both false, and both caused by the same root problem: #23 changed
what "urgent" means but didn't touch every artifact that encodes that
meaning.

**Failure 1.** `groups_into_distinct_priority_categories` scored `0.0`. Judge:
*"While 'Action Needed' is present, the 'Urgent' category is missing."* This
rubric predates #23 and still required Urgent to always be present — a
generic-time-sensitivity-era assumption that made sense when almost anything
could be Urgent, but not under the adopted taxonomy, where Urgent means a
job-search deadline and a run with no such deadline should correctly omit the
category. It directly contradicted its own sibling rubric,
`urgent_is_job_search_deadline_shaped`, which already said "if any." #23's
commit rewrote the routing rubrics for the *content* of Urgent (security
alerts don't belong there) but missed this rubric's separate, older
assumption about Urgent's *presence*.

**Failure 2.** `final_response_match_v2` scored `0.0` because the reference
response in `routing/routing_classification.test.json` still read *"A
security alert about a new sign-in to your account needs immediate
attention, along with any verification codes that have come in"* under
**Urgent** — the exact pre-#23 taxonomy, word for word. A correct response
under the new taxonomy can never match a reference written for the old one;
the fix isn't a moving target, the reference was just never updated.

**The pattern.** #23's commit updated `tools/classify_email.py`'s prompt,
`classification_agent`'s instructions, `agent_spec.yaml`, and the routing
rubrics — every artifact that was *top of mind* for "what urgent means" — but
missed two more that encode the same taxonomy less obviously: a rubric about
category *presence* (as opposed to content) and a reference response that
embeds a worked example. Same class of bug as "Stale references aren't just a
trajectory problem" in `evals/README.md`: a specification change has to
propagate to every artifact that encodes it, and a fixed reference response
or an old rubric is such an artifact even though neither looks like
"documentation of the taxonomy" on its face. Fixed by rewriting the rubric to
require categorized structure without requiring any specific category
populated, and rewriting the reference response to be consistent with the
adopted taxonomy while staying shape-based. `evals/README.md` now lists all
five artifacts a taxonomy change touches, specifically so the next change
doesn't repeat this.

---

### 25. Shape-based references can't cover data-dependent volume

Live run 2026-08-09, tracker staging, after the shape-based fix from #22 had
been in place. `rubric_based_final_response_quality_v1` scored `1.0` on all
three rubrics (states entries staged, states nothing written yet, asks for
confirmation) and `hallucinations_v1` scored `1.0` — all 36 staged entries
were grounded in the 51 emails fetched. The agent was correct.
`final_response_match_v2` scored `0.0` anyway.

**Why, given that #22 already fixed this class of failure.** #22's fix
rewrote the reference to describe shape instead of content — no named
company, role, date, or count — on the theory that a reference naming
nothing inbox-specific can't go stale. That held for the routing case, whose
response is a fixed handful of category buckets regardless of inbox size.
It didn't hold here, because tracker staging's response itemises every
staged entry, and the *number* of entries is itself inbox-dependent — 36
this run, some other count next time. `final_response_match_v2` is binary:
the judge returns VALID or INVALID for the response as a whole
(`google/adk/evaluation/final_response_match_v2.py:133-137`), so a
three-sentence reference describing the shape of a correct reply can never
be judged a match against a 36-item itemisation, no matter how correct
every one of those 36 items is. #22 fixed the case where the reference's
*content* went stale; it did not, and could not, fix the case where the
response's *length* is the variable.

**The general rule.** A fixed reference — content-based or shape-based —
can encode structure but not volume. It works when a response's shape is
stable regardless of the data behind it (routing, inbox listing, drafting,
tracker preview all qualify — see `evals/README.md`'s metrics table). It
fails whenever a correct response's length scales with live data, because
matching requires committing to a length in advance and no single reference
can commit to all of them. In that situation only rubric-based scoring
(independent yes/no questions about properties of the response, not a
match against the whole thing) can judge correctness. Fix:
`final_response_match_v2` removed from `evals/tracker/test_config.json`;
`tracker/tracker_staging.test.json` is now judged by
`rubric_based_final_response_quality_v1` and `hallucinations_v1` alone. The
reference response in the `.test.json` was left in place — it still
documents the intended shape of a correct reply, even with no metric
scoring against it. See "Shape-based references have a limit:
data-dependent volume" in `evals/README.md` for the three-data-point
argument this entry summarizes.

### 26. An additive instruction that didn't say it was additive

Live run 2026-08-09 15:33, drafting case, immediately after adding the
no-reply caveat from #13's follow-on work. Asked to draft a reply to the
Cisco rejection — sent from a post-only address — the agent's entire final
response was the "Replying to" line followed by *"This email is a post-only
address, so a reply is not possible. A reply to this address may not be
received."* No draft appeared anywhere. `notes_no_reply_address` and both
tool-use rubrics scored `1.0` — the body was fetched, the rejection intent
was correctly determined and passed to `drafting_agent`. `acknowledges_rejection`
and `gracious_professional` both scored `0.0`, because there was no
acknowledgement in the response to judge; the judge's reasoning for the
former noted the response "*only* states the email is unreplyable."

**Root cause.** The no-reply caveat had been added as step (c) in
`root_agent`'s draft-presentation format — a new bullet, syntactically a
peer of "show the draft" and "ask to send" — with nothing in its wording
saying it was meant to run *alongside* those steps rather than *instead of*
them. Given "the address can't receive a reply" as a true fact, the model's
own inference — a draft is pointless if it can't be sent — was reasonable
enough that it filled the gap the instruction left open. Nothing in the
instruction contradicted that inference, so it wasn't a leap. The evidence
pointed at both layers: `drafting_agent`'s own instruction had the same
shape of gap for confirmation emails ("keep the reply brief **or** note
that no reply is typically needed" — an "or" that reads as permission to
substitute), so the fix had to close it in both agents, not just in
`root_agent`'s presentation step.

**Fix.** In both instructions, replaced the implicit peer-bullet phrasing
with an explicit additive/unconditional statement: `root_agent`'s step (c)
now says the note is "ADDITIVE ONLY" and lists that the draft is "always
produced and presented... regardless of whether the source address accepts
replies"; `drafting_agent`'s instruction now says outright that it "always
produces the requested draft text" and that reply-ability "is not your
concern." `agent_spec.yaml` updated to match on both sides. Regression
tests in `test_agent_instructions.py` assert the additive language is
present and, checked against the pre-fix commit (tagged
`pre-noreply-regression`), fail without it.

**The general rule.** A new requirement that must coexist with an existing
one — rather than replace it — has to say so explicitly. "Also do X" reads
differently to a model than a same-level bullet that happens to be true at
the same time as the others; the model has no way to know two instructions
are meant to compose rather than compete unless the wording says which.
Same failure shape as #14 (a fix for one thing broke another, adjacent
thing) and #22/#25 (an instruction correct in isolation failed once a
second true-but-independent condition — here, no-reply status — entered
the picture). The fix in every one of these cases was the same move: stop
implying the relationship between two requirements and state it.

---

## Additional questions worth answering cold

7. Why is "observed, not self-reported" the design rule for guards — and which
   two bugs does it summarise?
8. Why did removing the snippet from search results convert a misclassification
   bug into a fabrication bug?
9. Why is "exactly one tracker_agent call" the correct root-level trajectory
   assertion for the confirmation guarantee?
10. Why did the preview intent start staging, and what does it imply about how
    conflicting instructions resolve?
11. Why can a rubric and a reference response both encode the same taxonomy
    and still drift out of sync with each other?
