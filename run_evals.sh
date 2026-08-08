#!/usr/bin/env bash
#
# Runs the full ADK eval suite (evals/).
#
# Deliberately NOT part of CI: these cases make live Gmail and Gemini API
# calls, cost real money, and depend on live inbox contents, so results are
# non-deterministic across runs. Run this by hand when you want to check
# agent behavior, not on every push. See evals/README.md for what each case
# asserts and why it's judged the way it is.
#
# Usage:
#   ./run_evals.sh              # run all 5 cases
#   ./run_evals.sh drafting     # run only the named case

set -u

if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "GOOGLE_API_KEY is not set. Evals call the Gemini API and need it." >&2
  exit 1
fi

# name:eval_file:config_file
CASES=(
  "inbox_listing:evals/inbox_listing.test.json:evals/test_config.json"
  "routing:evals/routing/routing_classification.test.json:evals/routing/test_config.json"
  "drafting:evals/drafting/drafting_rejection.test.json:evals/drafting/test_config.json"
  "tracker:evals/tracker/tracker_staging.test.json:evals/tracker/test_config.json"
  "tracker_preview:evals/tracker_preview.test.json:evals/test_config.json"
)

ONLY="${1:-}"

names=()
results=()
metrics_list=()
log_files=()
any_failed=0
ran_any=0

cleanup() {
  rm -f "${log_files[@]:-}"
}
trap cleanup EXIT

for entry in "${CASES[@]}"; do
  name="${entry%%:*}"
  rest="${entry#*:}"
  eval_file="${rest%%:*}"
  config_file="${rest#*:}"

  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then
    continue
  fi
  ran_any=1

  echo "=============================================================="
  echo "Case: $name"
  echo "  eval file:   $eval_file"
  echo "  config file: $config_file"
  echo "=============================================================="

  log_file="$(mktemp "${TMPDIR:-/tmp}/run_evals.${name}.XXXXXX")"
  log_files+=("$log_file")

  # `adk eval` exits 0 regardless of pass/fail, so the exit code below is
  # NOT a valid signal — pass/fail is determined by parsing ADK's own
  # "Overall Eval Status" verdict line out of the captured output instead.
  adk eval eval_agent "$eval_file" \
      --config_file_path="$config_file" \
      --print_detailed_results 2>&1 | tee "$log_file"

  # A depleted API quota does not recover mid-run, and each remaining case
  # would burn ~20 minutes of exponential backoff before giving up, so stop
  # the whole run here instead of grinding through the rest.
  if grep -q "429" "$log_file" && grep -q "RESOURCE_EXHAUSTED" "$log_file"; then
    echo
    echo "==============================================================" >&2
    echo "ABORTING: case '$name' hit 429 RESOURCE_EXHAUSTED - API quota is" >&2
    echo "depleted. Not continuing to remaining cases: a depleted quota will" >&2
    echo "not recover mid-run, and each case burns ~20 minutes of" >&2
    echo "exponential backoff before giving up. Wait for quota to reset, or" >&2
    echo "raise it, then re-run." >&2
    echo "==============================================================" >&2
    exit 1
  fi

  # ADK sometimes prints "Overall Eval Status: FAILED" for a case where
  # inference never actually ran (a metric came back NOT_EVALUATED, or a
  # 429 hit without triggering full quota exhaustion above) - that's not a
  # real failed assertion, so classify it as ERROR instead of FAILED.
  if grep -q "429" "$log_file" || grep -q "NOT_EVALUATED" "$log_file"; then
    status="ERROR"
    any_failed=1
  elif grep -q "Overall Eval Status: FAILED" "$log_file"; then
    status="FAILED"
    any_failed=1
  elif grep -q "Overall Eval Status: PASSED" "$log_file"; then
    status="PASSED"
  else
    status="ERROR"
    any_failed=1
  fi

  names+=("$name")
  results+=("$status")
  metrics_list+=("$(grep '^Metric: ' "$log_file" || true)")
done

if [ "$ran_any" -eq 0 ]; then
  echo "No case matched '$ONLY'." >&2
  echo "Known cases: inbox_listing, routing, drafting, tracker, tracker_preview" >&2
  exit 1
fi

echo
echo "=============================================================="
echo "Summary"
echo "=============================================================="
for i in "${!names[@]}"; do
  printf '  %-16s %s\n' "${names[$i]}" "${results[$i]}"
  if [ -n "${metrics_list[$i]}" ]; then
    while IFS= read -r line; do
      printf '      %s\n' "$line"
    done <<< "${metrics_list[$i]}"
  fi
done

if [ "$any_failed" -ne 0 ]; then
  echo
  echo "One or more cases FAILED or ERRORed - see above." >&2
fi

exit "$any_failed"
