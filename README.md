# Email Intelligence Agent

[![CI](https://github.com/PranavaRavindran/Email-Agent/actions/workflows/ci.yml/badge.svg)](https://github.com/PranavaRavindran/Email-Agent/actions/workflows/ci.yml)

A multi-agent system built on Google's Agent Development Kit (ADK) that reads a
Gmail inbox, classifies and prioritises messages, drafts replies for review, and
maintains a job application tracker in Google Sheets.

Built with Gemini 2.5 Flash, the Gmail API, and the Google Sheets API.

---

## What it does

```
You: what emails need my attention right now
You: draft a reply to the most recent email from Cisco
You: show me what you would add to my tracker from june and july 2026, but don't write anything
You: update my job application tracker with application emails from june and july 2026
```

The tracker is the substantial piece. Given a date range, it searches Gmail,
reads every matching message in full, groups them by company and role, determines
each application's current status and original application date, computes a diff
against the existing sheet, shows that diff, and writes only after confirmation.

---

## Architecture

```
                        root_agent
                    (orchestration only)
                            |
        +-----------+-------+--------+-------------+
        |           |                |             |
   inbox_agent  classification   drafting      tracker_agent
                   _agent        _agent
        |           |                |             |
   list_emails   classify_email  draft_reply   search_email_ids
   search_emails                               get_email_detail
   get_email_detail                            stage_write
                                               commit_write
```

`root_agent` holds no tools of its own and never touches email directly.
Sub-agents are connected via `AgentTool` rather than ADK's `sub_agents`
parameter, which keeps `root_agent` in control of the conversation and lets it
chain calls — routing "what needs my attention" through `inbox_agent` to fetch
and then `classification_agent` to prioritise.

Both `root_agent` and `tracker_agent` run at `temperature=0`.

### Why the tracker's tools are shaped the way they are

**`search_email_ids` returns message IDs and nothing else.** No subject, sender,
date, or preview text. Earlier versions returned metadata, and the agent
repeatedly classified emails from that metadata instead of opening them —
recording subject lines as job titles and inventing dates. Instructions telling
it to open every email were not sufficient. Removing the alternative was.

**`get_email_detail` extracts bodies from HTML when no plain-text part exists.**
Roughly 40% of recruiting email in this inbox is HTML-only, with the content
nested several levels deep in the MIME tree. A plain-text-only extractor returns
an empty string for all of them, and the agent then classifies from whatever
fragment it can find — which produced a confidently wrong "Applied" for an email
that was plainly a rejection. Bodies are truncated to 2000 characters; status is
always established in the opening paragraph, and untruncated bodies caused
rate-limit failures.

**`stage_write` and `commit_write` are separate tools.** `stage_write` resolves
entries against the sheet, computes a diff, and persists the resolved plan to
ADK session state without writing anything. `commit_write` takes **no
model-facing arguments** — it applies the persisted plan. Nothing large passes
back through the model between turns, so what you approve is what gets
written.

---

## The confirmation flow

```
You: update my job application tracker with application emails from june and july 2026

  ... reads 48 emails ...

  [stage_write] 5 new, 3 status changes, 28 unchanged, 0 duplicates in batch,
                0 existing rows not seen, 0 invalid entries

Agent: 5 new applications, 3 status changes. Write these to the sheet?

You: yes

  Done: 5 added, 3 updated
```

If nothing would change, it reports that and does not ask.

Confirmation lives in `root_agent`, not in `tracker_agent`. A sub-agent invoked
via `AgentTool` completes and terminates — it cannot pause mid-invocation and
wait for an answer, because the reply arrives at a fresh invocation with no
memory of the first. Staging to disk is what makes the two-turn flow work.

---

## Validation

Correctness checks run in code rather than being requested of the model, because
the model does not reliably follow them. Every run reports:

| Check | Catches |
| --- | --- |
| `duplicates in batch` | The same application extracted twice — merged, keeping earliest date and latest status |
| `existing rows not seen` | Rows already in the sheet that this run never examined |
| `invalid entries` | Missing fields, malformed dates, statuses outside the permitted four, dates outside the searched range |
| fetch refusal | Entries staged without any email having been read |

Two of these are **observed rather than self-reported**, and that distinction
matters.

An earlier version asked the agent to pass in how many emails it had searched and
fetched. The agent omitted those arguments, the check silently did not run, and a
live run staged 35 entirely fabricated job applications — plausible-looking
company names, zero emails read. Nothing reached the sheet only because the
confirmation step was waiting.

The tools now keep their own records: `search_email_ids` records what it
returned, `get_email_detail` records what it fetched, and `stage_write` compares
them directly, refusing to stage anything if no emails were read. The searched
date range is likewise parsed from the issued query rather than taken from the
agent, after an agent-supplied range caused three legitimate applications to be
discarded.

A guard that asks the component it is guarding for evidence is not a guard.

Other invariants enforced in code: sorting happens in `write_to_sheet`, not in
the agent; company and role are matched on a normalised comparison key so name
drift between runs doesn't create duplicate rows.

Every `stage_write` call appends a record to `run_log.jsonl` (fetch/search
counts, staged counts, whether the guard refused). `check_drift.py` reads
that log and flags real runs where the guard should have refused but didn't
— observability on production runs, distinct from the evals below, which
only check scenarios written in advance. Run it with `python check_drift.py`.

---

## Testing

Two layers, testing different things.

**`tests/` — pytest. Fast, no network.** 39 unit tests covering normalisation,
company and role matching, entry validation, duplicate merging, and sorting. Each
regression test is labelled with the bug it encodes.

```
python -m pytest tests/ -v
```

**`evals/` — ADK evaluation. Slow, live API calls.** Verifies agent behaviour:
which sub-agents get called, and whether the response makes the right claims.
Cases live in per-case subdirectories, each with its own criteria config. Run
the whole suite with:

```
./run_evals.sh              # all 5 cases
./run_evals.sh drafting     # just one case, by short name
```

See `evals/README.md` for all cases, their configs, and the metrics each one
uses (including LLM-judged rubric and hallucination checks added to catch
regressions that similarity-based scoring misses).

The split exists because ADK's response scoring cannot check extraction
correctness. Its default metric is ROUGE word overlap against a reference, which
cannot distinguish a correct status from an incorrect one — a run with every
status wrong would still score highly, because the company names match. Status
and date correctness is covered by the unit tests instead. Where a case needs
semantic judgement, `final_response_match_v2` (an LLM judge) is used rather than
word overlap.

Cases that assert behaviour inside a sub-agent are judged on the response rather
than the tool trajectory, because `root_agent`'s trajectory shows which
sub-agents it called but not which tools those sub-agents used.

---

## Setup

Requires Python 3.11+, a Google Cloud project with the Gmail and Sheets APIs
enabled, and a Gemini API key.

```
git clone <repo>
cd email-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export GOOGLE_API_KEY="your-key"
```

For linting, type checking, and running tests locally (the same checks CI
runs), also install `requirements-dev.txt`:

```
pip install -r requirements-dev.txt
```

Then `./scripts/check.sh` runs every quality gate (lint, format check, mypy,
pytest) in the same order CI does — CI invokes the same script, so passing it
locally means CI will pass.

Place your OAuth client secret at `credentials.json` in the project root. Both
`credentials.json` and the generated `token.json` are gitignored and must never
be committed. (Older checkouts may also have a legacy `token.pickle`; it is
migrated to `token.json` automatically on first run and is gitignored too.)

Set `_SPREADSHEET_ID` in `tools/write_to_sheet.py` to your own sheet. The
`Tracker` tab expects headers in row 2 (Date, Company, Role, Link, Source,
Status) with data beginning at row 3.

```
python main.py
```

First run opens a browser for OAuth consent. Scopes requested are
`gmail.readonly` and `spreadsheets` — the system can read email and write to
sheets, and cannot send, modify, or delete mail.

If authentication fails after a long idle period, delete `token.json` (and any
leftover `token.pickle`) and rerun.

### Optional: MCP-backed fetch/read paths

`get_email_detail`'s fetch and `write_to_sheet`'s read of existing Tracker
rows can go through a [Google Workspace MCP
server](https://github.com/gemini-cli-extensions/workspace) instead of the
raw Gmail/Sheets API calls, as a programmatic client (its tools are not
exposed to the model). See `MCP_INTEGRATION.md` for the full architecture,
gate decisions, and dual-auth model. This is optional — leaving it unset
uses the MCP path by default; setting either flag to `"0"` reverts to the
raw API path this project has always used.

1. Clone and build the server once:
   ```
   git clone https://github.com/gemini-cli-extensions/workspace ~/.workspace-mcp
   cd ~/.workspace-mcp && npm install && npm run build
   ```
   Requires Node.js >= 20. `tools/mcp_client.py` looks for it at
   `WORKSPACE_MCP_DIR` (env var, default `~/.workspace-mcp`) and fails with
   build instructions if `workspace-server/dist/index.js` is missing.
2. Two independent kill switches, both default `"1"` (MCP on):
   ```
   export USE_MCP_GMAIL=0    # revert get_email_detail's fetch to the raw Gmail API
   export USE_MCP_SHEETS=0   # revert write_to_sheet's sheet read to the raw Sheets API
   ```
3. Verify the server's tool allowlist took effect (no auth needed):
   ```
   python scripts/mcp_verify.py
   ```
4. One-time, user-run only (never run this from an agent): smoke-test an
   actual `gmail_get` and `sheets_getRange` call. First run opens a browser
   for the MCP server's **own** OAuth consent — separate from this
   project's `credentials.json`/`token.json`, stored in the OS keychain.
   Watch the consent screen and confirm it asks for read-only Gmail and
   Sheets access only, then abort if it asks for anything broader:
   ```
   python scripts/mcp_smoke.py <a-gmail-message-id>
   ```

### Running this somewhere other than a local machine

The setup above assumes one interactive process on one machine with a
browser. See `DEPLOYMENT.md` for the four things that assumption breaks in
a deployed environment — pending-write persistence, headless auth, Vertex
AI instead of an API key, and the MCP integration's stdio/Node/Keychain
coupling — how each is handled, and the full environment variable matrix
for local vs. deployed.

---

## Project layout

```
agent.py                    root_agent — orchestration and confirmation flow
main.py                     CLI entry point and session loop
auth.py                     OAuth, cached service handle
agent_spec.yaml             behavioural contract for the system

agents/
  inbox_agent.py            fetching and searching
  classification_agent.py   priority, action items, deadlines
  drafting_agent.py         reply drafting, outcome-aware
  tracker_agent.py          job application extraction

tools/
  list_emails.py            recent inbox messages
  search_emails.py          query search with metadata (inbox_agent only)
  search_email_ids.py       query search, IDs only (tracker_agent only)
  get_email_detail.py       full message, MIME-tree body extraction
  classify_email.py
  draft_reply.py
  write_to_sheet.py         normalisation, matching, staging, commit
  genai_client.py           shared Gemini client factory (API key or Vertex)
  mcp_client.py             MCP stdio client for the Workspace MCP server

scripts/
  mcp_verify.py             no-auth proof the MCP tool allowlist took effect
  mcp_smoke.py              one-time, user-run MCP call smoke test (needs OAuth)

tests/                      pytest unit tests
evals/                      ADK evaluation cases, one subdirectory per case
eval_agent/                 thin wrapper exposing root_agent to `adk eval`
MCP_INTEGRATION.md          MCP server architecture, gates, dual-auth model
DEPLOYMENT.md               portability blockers and env var matrix for deployment
```

---

## Engineering notes

`ENGINEERING_LOG.md` records the significant bugs in this project — the symptom,
the theories that turned out to be wrong and the evidence that ruled each one
out, the actual cause, and the fix.

The most instructive one: a job application showed as "Applied" when its only
email was a rejection. Four rounds of work went into the status-classification
logic before a debug print revealed the email's extracted body was zero
characters long. The classification was correct; the input was empty. Fixing
extraction silently corrected seven other applications that had been wrong
without anyone noticing.

`FUTURE_WORK.md` covers deferred work and known limitations, with the reasoning
for each deferral.
