# Design: classify-email-batching

## Context

`tools/classify_email.py` makes one blocking `generate_content` call per email with `response_mime_type: application/json` and falls back to a safe default (`fyi`, no items, no deadline) on any error. `classification_agent` iterates the tool over ~20 fetched emails, and each tool call also costs an agent turn to emit the args — roughly 40 sequential model calls for 20 emails, confirmed serial in a local `main.py` run (the `[classify_email]` lines print one at a time). Completeness is currently enforced only by instruction (the agent's count-check rule). `classify_email` is one of the 9 model-facing tool signatures, so its contract change must be validated with a live eval run, not just pytest. See proposal.md for motivation.

**Hard constraint:** the exact prompt each email's text flows through must not change. The per-email prompt (criteria, examples, formatting) is tuned and eval-verified; this change alters orchestration only.

## Goals / Non-Goals

**Goals:**
- One tool invocation classifies the whole batch; the per-email model calls run concurrently inside the tool.
- Make per-email iteration structurally impossible to need: the tool takes the list, so there is no per-email loop for the agent to mismanage, and no per-email agent turns.
- Byte-identical per-email prompt; classification behavior for any individual email is unchanged.
- Preserve the existing safe-default fallback per email.

**Non-Goals:**
- No change to how emails are fetched or how many (root_agent's default of 20 stays), and no content fetching inside the tool.
- No change to the per-email prompt, criteria, or the grouped summary output format.
- No caching, dedup, or persistence of classification results.
- No change to the MCP/raw-API selection (USE_MCP_* untouched — classification never used them).
- No `thinking_config` change. Recorded observation: `classify_email` runs `gemini-2.5-flash` with no `thinking_config`, so dynamic thinking is on and pads every call. That is a separate latency lever deserving its own eval-gated change; bundling it here would confound the eval results for batching.

## Decisions

**1. Bulk fan-out of the unchanged per-email prompt, not one batched prompt.**
The tool takes the list and runs the existing per-email classification concurrently inside the tool; each email's text flows through the byte-identical prompt used today. Alternatives considered:
- *Single batched prompt (one model call for all 20)* — rejected: it changes the prompt every email flows through, and batch context can shift individual classifications. The eval suite (5 cases) is too small a safety net to re-tune classification behavior when the goal is orchestration latency, and it adds response-alignment failure modes (a dropped array item shifts every later classification onto the wrong email).
- *IDs-only handoff, where the tool fetches content itself* — rejected: it changes what text the classifier sees (full body vs. the snippet passed today), which can change categories. The caller keeps determining classifier input (now a spec requirement).

**2. Extract the current single-email path into an internal helper; the batch tool wraps it.**
The prompt construction, model call, parsing, and safe-default fallback move verbatim into a private per-email function. The public tool fans out over it. This makes prompt invariance testable in pytest: a unit test can assert the helper's prompt for a given email is byte-identical to the pre-change prompt.

**3. Concurrency via a bounded thread pool.**
The `google-genai` client call is blocking and ADK tools here are sync, so `ThreadPoolExecutor` with a modest worker cap (8) is the natural fit — no async rewrite, no event-loop coupling. `max_workers=8` matches `get_emails_bulk`'s existing pool (`_MAX_WORKERS = 8` in `tools/get_emails_bulk.py`) rather than introducing a second arbitrary cap for the same shape of concurrent per-email work; at the default batch of 20 the fan-out runs in three waves, trading a little latency for headroom against Vertex rate limits. Alternative considered: unbounded workers (fastest, but a 20-way burst invites 429s, and a rate-limited email silently becomes `fyi` via the fallback — accuracy paid for speed).

**4. Rename the tool to `classify_emails(emails: list[dict]) -> dict`.**
A tool named `classify_email` that takes a list would mislead the calling model. Both `agents/classification_agent.py` and `agent_spec.yaml` are edited anyway, so the rename adds no extra blast radius. Alternative considered: keep the name and only change the parameter — less churn in prose references, but leaves a permanently misleading name in the model-facing surface.

**5. Return shape: `{"results": [...]}` — a dict wrapping an ordered list, one entry per input email.**
Keeps the ADK tool return type a dict (matching every other tool here), while the list carries order. Each entry has the existing keys (`classification`, `action_items`, `deadline`) plus `index` and `subject` echoed back so the agent can attach results to emails without relying on position alone. Alignment is by construction — each worker's result is written to its input's slot — so there is no model-output alignment to parse, unlike the batched-prompt alternative.

**6. Failure fallback stays per-email.**
The helper's existing try/except-to-safe-default behavior carries over unchanged, so one failed or unparseable call defaults that email alone; the tool never raises, even if every call fails.

**7. Agent instruction: call once, keep the completeness guard.**
`classification_agent`'s instruction changes from per-email framing to "pass all emails to `classify_emails` in one call", keeping the existing count-reconciliation rule as the last-line guard. The structural fix (one call, one result per email) does the heavy lifting; the instruction guard catches summary-stage drops.

## Risks / Trade-offs

- [Concurrent burst triggers Vertex rate limiting, and a 429'd email silently defaults to `fyi`] → worker cap of 8 bounds the burst; the per-email fallback bounds the damage to the affected email; live eval run confirms the happy path end to end.
- [Fan-out changes behavior in some subtle way despite the unchanged prompt] → prompt invariance is pytest-enforced (byte-identical assertion via the extracted helper), and `./run_evals.sh` is the acceptance gate before merge — mandatory, since pytest cannot verify a model-facing signature change.
- [Rename breaks prose references to `classify_email`] → `agent_spec.yaml`, agent instructions, `evals/README.md`, and tests are all updated in this change; `grep -rn classify_email` (excluding `openspec/changes/`) must come back empty.
- [No runtime rollback flag] → Rollback is `git revert` of the single change; the tool is stateless so reverting is safe. A kill switch was considered and rejected: unlike the MCP switches this is not an external-dependency boundary, and dual code paths would double the eval surface.

**Found during live verification (2026-08-13), not anticipated above:** the concurrent fan-out raises the request rate enough that a full eval suite run can exhaust free-tier Gemini quota — a re-run of a single eval case aborted on 429 RESOURCE_EXHAUSTED after the suite had run. The worker cap bounds the burst within one batch, but back-to-back eval runs on free-tier quota are now a real constraint; this is a standing trade-off of the change, not a regression in any one case.

## Migration Plan

1. Land tool + agent + spec/contract + test updates as one change (the old and new signatures cannot coexist under one tool name).
2. Verify with `./scripts/check.sh`, then a mandatory live eval run `./run_evals.sh` (signature change is not pytest-verifiable).
3. Rollback: `git revert` the change commit; no data or state migration exists.
