# Proposal: classify-email-batching

## Why

Answering "what needs my attention?" fetches ~20 emails, then classification_agent calls `classify_email` once per email. Each call is a blocking `generate_content` call, and each also costs an agent turn to emit the tool-call args — roughly 40 sequential model calls for 20 emails (confirmed serial: the `[classify_email]` lines print one at a time in a local `main.py` run). That makes the most common request the slowest and most expensive one, and each extra tool round-trip is another chance for the agent to stop early or drop an email. A bulk fan-out tool removes that path structurally, matching the project's guiding principle (constrain structurally, like batched fetching already does elsewhere).

## What Changes

- **BREAKING (tool signature)**: the classification tool accepts a batch — a list of emails — and returns one result per input email, in input order, instead of taking a single email dict. It is one of the 9 model-facing tool signatures, so this cannot be verified by pytest alone and requires a live eval run (`./run_evals.sh`).
- The tool fans out concurrently inside the tool: each email still flows through the **byte-identical per-email prompt** used today; only the call orchestration changes. The agent makes one tool call instead of twenty, and the twenty classification calls run concurrently instead of serially.
- Per-email failure isolation is preserved: an email whose classification call fails or cannot be parsed still gets the safe default (`fyi`, no action items, no deadline) without discarding the rest of the batch.
- `classification_agent`'s instruction is updated to call the tool once with all emails rather than iterating, and its completeness guard (every input email accounted for) is kept.
- Classification criteria, categories (`urgent`, `action_needed`, `fyi`, `spam`), the per-email prompt, and the grouped summary output format are unchanged.

## Capabilities

### New Capabilities

- `email-classification`: Priority classification of fetched emails — categories and criteria, batched single-call tool contract, per-email fallback behavior, classification-input invariance (the tool classifies the content it is given), and the completeness guarantee (every input email classified exactly once). This is the project's first spec; it covers the classification capability as it will exist after this change.

### Modified Capabilities

<!-- none — no main specs exist yet -->

## Impact

- `tools/classify_email.py`: signature change (single email → list of emails, one result per email) and concurrent fan-out; the per-email prompt itself is untouched.
- `agents/classification_agent.py`: instruction updated for single-batch tool use.
- `agent_spec.yaml`: `classify_email` tool contract entry updated.
- `evals/README.md`: references updated.
- Tests under `tests/`: new unit coverage for the batch tool, including a guard that the per-email prompt is byte-identical to today's.
- Verification: `./scripts/check.sh` for static/unit coverage, plus a mandatory live eval run (`./run_evals.sh`) because a model-facing tool signature changes.
- No new dependencies; no change to Gmail/Sheets paths or the USE_MCP_* kill switches.
- Recorded, out of scope: `classify_email` runs `gemini-2.5-flash` with no `thinking_config`, so dynamic thinking is on and pads every call — a separate latency lever worth its own change.
