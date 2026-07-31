# ADK evaluation

## Running

```
adk eval eval_agent evals/tracker_staging.test.json --config_file_path=evals/test_config.json --print_detailed_results
```

Run from the project root. `eval_agent` is a thin wrapper package (see
`eval_agent/`) that exposes the real `root_agent` from the project root's
`agent.py` under the module name ADK expects; nothing about the agent itself
is duplicated or changed.

## Why this isn't run automatically

This eval makes real Gmail API calls (via `search_email_ids` and
`get_email_detail`) and a real Google Sheets staging call (via `stage_write`).
It's slow and depends on live inbox contents, so it's run deliberately by a
person rather than on every change, unlike the fast, mocked tests in
`tests/`.

## What it asserts

The eval checks the tool trajectory for the eval case
`tracker_stages_without_committing`: that `search_email_ids`,
`get_email_detail`, and `stage_write` are called, and — just as
important — that `commit_write` is NOT called. That's the confirmation
guarantee: a tracker update request should stage changes for review, never
write them to the sheet on its own.

## What it does NOT assert

It does not assert that the extracted roles, companies, statuses, or dates
are correct. ADK's `response_match_score` is ROUGE-1 word overlap between the
agent's final response and the reference response, which can't tell a correct
status apart from an incorrect one — it only rewards shared words. That's why
`response_match_score` is set low (0.3) here: it's not a meaningful
correctness signal for this agent, just a loose sanity check so it doesn't
cause false failures. Correctness of extracted statuses and dates is covered
instead by `tests/test_write_to_sheet.py`.
