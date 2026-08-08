"""End-to-end checks for run_evals.sh's pass/fail parsing.

`adk eval` exits 0 regardless of whether the eval it ran passed or failed
(confirmed against a live routing_classification run), so run_evals.sh must
determine PASSED/FAILED/ERROR by parsing ADK's own "Overall Eval Status"
line out of the captured output rather than trusting $?. These tests stub
out `adk` with a fake executable that always exits 0 and replays either a
real captured FAILED block, a real captured PASSED block, or garbage with
no verdict line at all, then assert run_evals.sh reports the right status
and exit code for each.
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_EVALS = REPO_ROOT / "run_evals.sh"

# Captured verbatim from a real `adk eval` run of
# evals/routing/routing_classification.test.json that printed
# "Overall Eval Status: FAILED" but exited 0 - the exact bug this script
# guards against.
CAPTURED_FAILED_BLOCK = """\
*********************************************************************
Eval Run Summary
routing_classification:
  Tests passed: 0
  Tests failed: 1
********************************************************************
Eval Set Id: routing_classification
Eval Id: attention_request_routes_through_classification
Overall Eval Status: FAILED
---------------------------------------------------------------------
Metric: final_response_match_v2, Status: FAILED, Score: 0.0, Threshold: 0.8
---------------------------------------------------------------------
"""

CAPTURED_PASSED_BLOCK = """\
*********************************************************************
Eval Run Summary
some_case:
  Tests passed: 1
  Tests failed: 0
********************************************************************
Eval Set Id: some_case
Eval Id: some_eval_id
Overall Eval Status: PASSED
---------------------------------------------------------------------
Metric: final_response_match_v2, Status: PASSED, Score: 1.0, Threshold: 0.8
---------------------------------------------------------------------
"""

# adk always exits 0 here, matching the real (buggy-to-rely-on) behavior.
FAKE_ADK = r"""#!/usr/bin/env bash
eval_file=""
for arg in "$@"; do
  case "$arg" in
    *.test.json) eval_file="$arg" ;;
  esac
done

if [ -n "${FAKE_ADK_ERROR_CASE:-}" ]; then
  case "$eval_file" in
    *"$FAKE_ADK_ERROR_CASE"*)
      echo "some unexpected crash output with no verdict line"
      exit 0
      ;;
  esac
fi

if [ -n "${FAKE_ADK_FAIL_CASE:-}" ]; then
  case "$eval_file" in
    *"$FAKE_ADK_FAIL_CASE"*)
      cat <<'BLOCK'
""" + CAPTURED_FAILED_BLOCK + """BLOCK
      exit 0
      ;;
  esac
fi

cat <<'BLOCK'
""" + CAPTURED_PASSED_BLOCK + """BLOCK
exit 0
"""


def _write_fake_adk(bin_dir):
    adk_path = bin_dir / "adk"
    adk_path.write_text(FAKE_ADK)
    mode = adk_path.stat().st_mode
    adk_path.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return adk_path


def _run(tmp_path, env_overrides):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_adk(bin_dir)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GOOGLE_API_KEY"] = "dummy-key-for-tests"
    env.update(env_overrides)

    return subprocess.run(
        ["bash", str(RUN_EVALS)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _summary_lines(stdout):
    # The real "Summary" banner is the last one printed; each case's fake
    # adk output also contains the words "Eval Run Summary", so anchor on
    # the final occurrence.
    idx = stdout.rindex("\nSummary\n")
    return stdout[idx:].splitlines()


def _case_line(summary_lines, case_name):
    return next(
        line
        for line in summary_lines
        if line.strip().split(" ", 1)[0] == case_name
    )


class TestRunEvalsPassFailDetection:
    def test_failed_case_is_not_reported_as_pass(self, tmp_path):
        result = _run(tmp_path, {"FAKE_ADK_FAIL_CASE": "routing"})
        summary = _summary_lines(result.stdout)

        routing_line = _case_line(summary, "routing")
        assert "FAILED" in routing_line
        assert "PASS" not in routing_line

    def test_failed_case_makes_exit_code_nonzero(self, tmp_path):
        result = _run(tmp_path, {"FAKE_ADK_FAIL_CASE": "routing"})
        assert result.returncode != 0

    def test_passing_cases_are_still_reported_as_passed(self, tmp_path):
        result = _run(tmp_path, {"FAKE_ADK_FAIL_CASE": "routing"})
        summary = _summary_lines(result.stdout)

        tracker_line = _case_line(summary, "tracker")
        assert "PASSED" in tracker_line

    def test_missing_verdict_line_is_error_not_pass(self, tmp_path):
        result = _run(tmp_path, {"FAKE_ADK_ERROR_CASE": "drafting"})
        summary = _summary_lines(result.stdout)

        drafting_line = _case_line(summary, "drafting")
        assert "ERROR" in drafting_line
        assert "PASS" not in drafting_line
        assert result.returncode != 0

    def test_metric_lines_are_included_in_summary(self, tmp_path):
        result = _run(tmp_path, {"FAKE_ADK_FAIL_CASE": "routing"})
        summary = "\n".join(_summary_lines(result.stdout))
        assert "Metric: final_response_match_v2, Status: FAILED, Score: 0.0, Threshold: 0.8" in summary

    def test_all_passing_exits_zero(self, tmp_path):
        result = _run(tmp_path, {})
        assert result.returncode == 0

    def test_output_is_streamed_not_swallowed(self, tmp_path):
        # Regression guard: the eval output itself (not just the summary)
        # must still reach stdout as it runs, not just at the end.
        result = _run(tmp_path, {"FAKE_ADK_FAIL_CASE": "routing"})
        assert "Eval Id: attention_request_routes_through_classification" in result.stdout
