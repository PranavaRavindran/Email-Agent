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
any_failed=0
ran_any=0

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

  if adk eval eval_agent "$eval_file" \
      --config_file_path="$config_file" \
      --print_detailed_results; then
    names+=("$name")
    results+=("PASS")
  else
    names+=("$name")
    results+=("FAIL")
    any_failed=1
  fi
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
done

exit "$any_failed"
