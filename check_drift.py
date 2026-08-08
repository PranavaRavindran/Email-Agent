"""Observability for tracker runs, not evaluation.

evals/ checks behavior against cases written in advance: a fixed set of
inputs with a known-good reference. This module instead inspects
run_log.jsonl, the record `stage_write` appends on every real invocation
(tools/write_to_sheet.py::_log_run) - including runs nobody anticipated and
wrote a case for. Evals answer "does the agent still pass the scenarios we
thought of"; this answers "did anything strange happen in production".

Every record is checked for two independent drift signals:

  ids_fetched < ids_searched   -> INCOMPLETE EXTRACTION
      Fewer emails were opened than the search returned. Entries staged from
      a run like this cannot all be grounded in email content that was
      actually read.

  entries_received > ids_fetched   -> FABRICATION RISK
      More entries were staged than emails were opened - entries exist that
      cannot have come from any opened email.

  (either of the above) AND refused == false   -> GUARD DID NOT FIRE
      The most severe verdict. stage_write has a code-level guard that is
      supposed to refuse staging in exactly these situations (see
      _FETCH_COMPLETENESS_ERROR and the entries-outnumber-fetches check in
      tools/write_to_sheet.py). A drift signal present alongside refused ==
      false means that guard did not catch it.

The committed run_log.jsonl contains a historical record from 2026-07-31
(entries_received=25, ids_fetched=5, ids_searched=48, refused=false) that
predates the fetch-completeness guard being tightened to also compare
entries_received against ids_fetched. This tool is expected to flag that
record as GUARD DID NOT FIRE. That is correct behavior, not a bug in the
detector - it is the fabrication signature described in README.md and
ENGINEERING_LOG.md, preserved as a fixture that validates this tool still
catches it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

_RUN_LOG_PATH = "run_log.jsonl"

_REQUIRED_FIELDS = ("ts", "entries_received", "ids_searched", "ids_fetched", "refused")

INCOMPLETE_EXTRACTION = "INCOMPLETE EXTRACTION"
FABRICATION_RISK = "FABRICATION RISK"
GUARD_DID_NOT_FIRE = "GUARD DID NOT FIRE"
CLEAN = "CLEAN"


@dataclass
class Record:
    ts: str
    entries_received: int
    ids_searched: int
    ids_fetched: int
    refused: bool
    verdict: str

    @property
    def flagged(self) -> bool:
        return self.verdict != CLEAN

    @property
    def severe(self) -> bool:
        return self.verdict == GUARD_DID_NOT_FIRE


def classify(entries_received: int, ids_searched: int, ids_fetched: int, refused: bool) -> str:
    """Returns the verdict string for one record's counts."""
    flags = []
    if ids_fetched < ids_searched:
        flags.append(INCOMPLETE_EXTRACTION)
    if entries_received > ids_fetched:
        flags.append(FABRICATION_RISK)

    if not flags:
        return CLEAN
    if not refused:
        return GUARD_DID_NOT_FIRE
    return " + ".join(flags)


def _parse_line(line: str) -> Record | None:
    """Parses one run_log.jsonl line into a Record, or None if the line is
    malformed, truncated, or missing a required field. Never raises."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if not all(field in obj for field in _REQUIRED_FIELDS):
        return None

    try:
        entries_received = int(obj["entries_received"])
        ids_searched = int(obj["ids_searched"])
        ids_fetched = int(obj["ids_fetched"])
        refused = bool(obj["refused"])
        ts = str(obj["ts"])
    except (TypeError, ValueError):
        return None

    verdict = classify(entries_received, ids_searched, ids_fetched, refused)
    return Record(
        ts=ts,
        entries_received=entries_received,
        ids_searched=ids_searched,
        ids_fetched=ids_fetched,
        refused=refused,
        verdict=verdict,
    )


def read_records(path: str, last: int | None = None) -> tuple[list[Record], int]:
    """Reads run_log.jsonl at path. Returns (records, malformed_count).

    Malformed or truncated lines are skipped and counted rather than raising.
    If last is given, only the most recent `last` valid records are returned
    (malformed_count still reflects the whole file).
    """
    with open(path) as f:
        lines = f.readlines()

    records = []
    malformed_count = 0
    for line in lines:
        record = _parse_line(line)
        if record is None:
            if line.strip():
                malformed_count += 1
            continue
        records.append(record)

    if last is not None:
        records = records[-last:]

    return records, malformed_count


def _print_table(records: list[Record]) -> None:
    header = (
        f"{'timestamp':<32} {'searched':>8} {'fetched':>8} {'received':>8} {'refused':>8}  verdict"
    )
    print(header)
    print("-" * len(header))
    for r in records:
        print(
            f"{r.ts:<32} {r.ids_searched:>8} {r.ids_fetched:>8} {r.entries_received:>8} "
            f"{str(r.refused):>8}  {r.verdict}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drift detector for run_log.jsonl (observability, not evaluation)."
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        help="Only inspect the most recent N records.",
    )
    parser.add_argument(
        "--path",
        default=_RUN_LOG_PATH,
        help=f"Path to the run log (default: {_RUN_LOG_PATH}).",
    )
    args = parser.parse_args(argv)

    try:
        records, malformed_count = read_records(args.path, last=args.last)
    except FileNotFoundError:
        print(f"No run log found at {args.path} - nothing to inspect.")
        return 0

    if records:
        _print_table(records)
    else:
        print("No records to inspect.")

    flagged = [r for r in records if r.flagged]
    severe = [r for r in records if r.severe]

    print()
    print(
        f"{len(records)} records read, {len(flagged)} flagged, "
        f"{malformed_count} malformed and skipped."
    )

    if severe:
        print(
            f"{len(severe)} record(s) show GUARD DID NOT FIRE - a drift signal "
            "that stage_write's own guard missed."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
