# Future Work

Deliberately deferred items, with the reasoning behind each. Ordered roughly by
value, not by effort.

---

## 1. MCP for transport, custom parsing retained

**The idea.** Use a Gmail MCP server for search and message retrieval, but keep
`_extract_body` for parsing. MCP handles fetching; project code handles reading.

**Why it isn't just "add MCP."** Roughly 40% of the emails in this inbox are
HTML-only, with no `text/plain` part anywhere in the MIME tree. A generic email
server extracts plain text and returns an empty body for those, which is
precisely the bug that made the tracker report a rejection as "Applied."
Swapping the custom tools for stock MCP tools would reintroduce it.

**The open question.** This design only works if the server exposes the *raw*
message payload. Most MCP email servers return a pre-parsed body string, which
means their extraction has already run and there is nothing left to parse. If a
server returns the raw payload, this is a genuinely good architecture. If not,
the wrapper would discard the server's output and re-fetch — worse than either
option alone.

**Why it's a good interview answer either way:** "I used MCP for transport and
kept my own parsing, because generic extraction dropped 40% of my email bodies"
is a real engineering trade-off, backed by a measured number.

**Not deferred because it's hard — deferred because evaluating unknown servers
under a deadline is how you lose an evening.**

---

## 2. Nightly cleanup agent (A2A + drift detection)

**What it does.** A separate scheduled process that reconciles the tracker
against the inbox overnight:

- Finds likely-duplicate rows the normalization missed
- Flags rows stuck at "Applied" for 90+ days with no follow-up
- Finds applications in Gmail that never made it into the sheet
- Finds sheet rows with no supporting email
- Reconciles the legacy manual tracker (`Sheet1`) against `Tracker`

**The critical design constraint: it proposes, it does not merge.** It writes
candidate pairs to a `Review` tab with its reasoning and stops. A wrong call
becomes a suggestion you decline, not a row that silently vanished. Auto-apply
above a confidence threshold is possible later, once it has a track record.

**Why this is the right home for LLM-based entity matching.** Every objection to
using model judgment for deduplication was about putting it on the *write path*:
nondeterministic, unreviewable, silently destructive, repeated every run. A
batch job inverts all four — it runs when nothing else is happening, it can be
slow and thorough, and its output is reviewable before anything changes.

**Why it's also the right home for A2A.** The current system is four agents in
one process connected by `AgentTool`; A2A would be contrived there. A scheduled
job in a separate process is a real second system — it has an Agent Card, it's
discoverable, it communicates over a network rather than a function call. The
justification is honest rather than manufactured.

**And for observability.** A nightly job that reconciles output against ground
truth *is* a drift detector. If classification quality changes after a model
update, this notices before a human does.

**Sequencing: after evaluation.** Otherwise it's an unverified agent auditing an
unverified agent.

---

## 3. Evaluation (ADK eval framework)

**Why it matters more than it sounds.** The project reached a state where two
consecutive runs produce byte-identical output across a process restart. That
stable baseline is exactly what a test suite should be built from — and it will
not survive the next round of changes. Test cases written now capture a known-good
state; written later, they capture whatever the system happens to do.

**Concrete first test cases**, all drawn from real bugs:

| Case | Asserts |
|---|---|
| Cisco rejection (HTML-only body, opens with "Thank you for applying") | Rejected, not Applied |
| Visa assessment invite | Interviewing — the rule that was undefined and flip-flopped |
| KeyBank (two most recent emails are feedback surveys) | Status comes from the last *status-bearing* email |
| Abbott (confirmation + later rejection) | One row, earliest date, latest status |
| Two IBM roles with similar titles | Two rows, never merged |
| Full re-run against a populated sheet | 0 added, N unchanged |

**The interview framing:** "You can't make an LLM deterministic. You constrain
what's allowed to vary, absorb the rest in code, and measure the remainder.
Evaluation is the measuring."

---

## 4. Deploy to Vertex AI Agent Engine

Managed runtime, no local process. Requires a GCP project, Vertex AI enabled,
and a staging bucket. Deferred because a local demo doesn't benefit from it and
first-time deploy loops reliably consume hours.

Optional follow-on: Docker + GKE, for the "I can containerize and orchestrate"
line rather than for any functional gain.

---

## 5. Replace pattern-matching in `_normalize` with similarity matching

**The current weakness, and it's real.** `_normalize` handles the specific drift
patterns observed across 48 emails: legal suffixes (`LLC`, `Inc`), parentheticals,
trailing location segments. Anything else slips through and creates a duplicate —
`IBM` vs `International Business Machines`, `&` vs `and`, a trailing `Careers` or
`Talent Team`. Its coverage is fixed and the gaps are invisible until one bites.

**Why hardcoded rules were still the right first choice.** Not because they're
more capable — because of *how they fail*. An unhandled pattern produces a
visible duplicate row: reproducible, one-line fix, never regresses on cases it
already handles. A model handles the unseen pattern but can also merge two things
that shouldn't merge, differently on different runs, silently. For an exact-match
lookup where a false merge destroys a row, the loud failure is the cheaper one.

**The better middle option.** Deterministic fuzzy matching — token overlap or
`difflib.SequenceMatcher` above a threshold. Generalizes beyond the hardcoded
list while staying identical every run. Wouldn't catch `IBM` vs
`International Business Machines`, but would catch ampersand variants, trailing
words, and word-order shifts.

**Specific known risk:** the rule that cuts everything after the first comma or
dash is the most aggressive line in the function. `Software Engineer, Full Stack`
reduces to `software engineer` — which would silently merge with a plain
`Software Engineer` role at the same company. It has not caused a bug yet, and it
is currently *load-bearing* (it's why two shortened IBM titles matched their
stored rows), so tightening it needs care.

---

## 6. PASS 2 — conditional backward lookback (removed by decision)

**Original design.** When no application confirmation email is found within the
requested range, run a second targeted search backwards over the prior three
months to recover the true application date.

**What was actually happening.** Debug output proved only one search ever ran.
PASS 2 had never executed in any session. Rows dated before the requested range
came from the primary search catching the boundary date, not from a lookback.

**The decision: removed from the design rather than implemented.** No row is
currently wrong because of it — when no confirmation exists, the date falls back
to the earliest email available, flagged approximate. The accuracy gain applies
only to applications older than the search window, and three other roadmap items
carry more weight.

**The reason this mattered enough to decide rather than ignore:** the spec and
notes described a two-pass design the code didn't have. Describing nonexistent
code in an interview is the failure mode. Building it or removing it both fix
that; leaving it was the only bad option.

---

## 7. Undecided: date rule for mixed-signal groups

When one company+role group contains both an interview email and a rejection,
two rules give different answers — "earliest email, flagged approximate" versus
"most recent update." Currently moot because no group in the dataset has both.
Needs deciding before it produces output that reads as wrong.

---

## 8. Smaller known issues

- **No pagination.** Gmail's `messages().list` is capped by `max_results`; a
  warning fires when `nextPageToken` is present, but results aren't paged.
  The warning is also noisy when a small limit was deliberately requested.
- **`agent_spec.yaml` is stale.** It describes a system that no longer exists —
  wrong tool lists, a guardrail that would reintroduce a fixed bug, and none of
  the tracker's actual design. Inert at runtime, but the codebase is scaffolded
  *from* it, so any regeneration would undo real work.
- **Cosmetic:** the input prompt occasionally prints twice (`You: You:`).
- **Resume bullet 2** claims "via Google Workspace MCP," which the project does
  not use. Needs rewording to describe the direct Gmail/Sheets API integration.
