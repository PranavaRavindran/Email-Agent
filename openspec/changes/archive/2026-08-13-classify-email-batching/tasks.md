# Tasks: classify-email-batching

## 1. Bulk fan-out classification tool

- [x] 1.1 Extract the current single-email path in `tools/classify_email.py` into a private per-email helper — prompt construction byte-identical, model call, JSON parsing, and safe-default fallback moved verbatim
- [x] 1.2 Add `classify_emails(emails: list[dict]) -> dict` that fans out over the helper with `ThreadPoolExecutor(max_workers=8)` (matching `get_emails_bulk`'s `_MAX_WORKERS`) and returns `{"results": [...]}` in input order, each entry echoing `index` and `subject` alongside `classification`, `action_items`, `deadline`
- [x] 1.3 Ensure the tool never raises: a failed or unparseable per-email call yields the safe default (`fyi`, `[]`, `""`) for that email only; all-calls-failed yields one safe default per input
- [x] 1.4 Update `tools/__init__.py` exports for the rename

## 2. Agent and contract updates

- [x] 2.1 Update `agents/classification_agent.py`: register `classify_emails`, change instruction to a single tool call with all emails, keep the count-reconciliation completeness rule and grouped output format unchanged
- [x] 2.2 Update `agent_spec.yaml`: rename the `classify_email` tool entry, describe the batch contract (list in, one result per email in input order, caller-provided content only, per-email safe-default fallback)
- [x] 2.3 Update `evals/README.md` references and run `grep -rn classify_email` (excluding `openspec/changes/`) to confirm no stale references remain

## 3. Tests

- [x] 3.1 Add a prompt-invariance test: assert the per-email helper's prompt for a fixture email is byte-identical to the pre-change prompt (snapshot the current prompt text before refactoring)
- [x] 3.2 Unit-test `classify_emails` with a mocked genai client: order preservation with concurrent completion out of order, single-email batch, one-failure isolation, all-calls-failed returning safe defaults, snippet-only email classified from its snippet, `index`/`subject` echo
- [x] 3.3 Run `./scripts/check.sh` (ruff, format, mypy, pytest) clean

## 4. Live verification (mandatory)

- [x] 4.1 Run `./run_evals.sh` (model-facing tool signature changed; pytest cannot verify it) and confirm classification cases still pass, including verification-code-is-fyi and interview-request-is-urgent
- [x] 4.2 Sanity-check latency in a local `main.py` run: the `[classify_email]` log lines should now appear near-simultaneously for a batch, not one at a time

### Verification results (2026-08-13)

- 4.1: `routing` passed 1.0/1.0 on both metrics — the case that asserts on classification categories and the only one exercising `classify_emails`, so prompt invariance held against the live model. `inbox_listing`, `drafting`, and `tracker` also passed. `tracker_preview` failed one wording rubric (`states_nothing_staged_or_written`) — unrelated to this change (it does not call `classify_emails`, and its `tool_trajectory_avg_score` was 1.0, so no staging occurred). A re-run of that case alone aborted on a 429 RESOURCE_EXHAUSTED before executing.
- 4.2: `[classify_email]` log lines now print in a burst of ~8 followed by a rolling stream as workers free up, consistent with `max_workers=8` over ~20 emails; previously they printed one at a time.
- Trade-off discovered during verification: concurrent fan-out raises the request rate enough that a full eval suite can exhaust free-tier Gemini quota (429 RESOURCE_EXHAUSTED). Not anticipated in design.md — see the note there.
