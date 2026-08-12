import json

from check_drift import (
    CLEAN,
    FABRICATION_RISK,
    GUARD_DID_NOT_FIRE,
    INCOMPLETE_EXTRACTION,
    STAGED_NO_COMMIT,
    classify,
    read_records,
)


def _write_log(tmp_path, lines):
    path = tmp_path / "run_log.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _record(
    ts="2026-08-01T00:00:00+00:00",
    entries_received=5,
    ids_searched=5,
    ids_fetched=5,
    refused=False,
):
    return json.dumps(
        {
            "ts": ts,
            "entries_received": entries_received,
            "new": entries_received,
            "status_changes": 0,
            "unchanged": 0,
            "duplicates_in_batch": 0,
            "unseen_rows": 0,
            "invalid_entries": 0,
            "ids_searched": ids_searched,
            "ids_fetched": ids_fetched,
            "refused": refused,
        }
    )


def _commit_record(ts="2026-08-01T00:05:00+00:00", rows_added=1, rows_updated=0):
    return json.dumps(
        {
            "ts": ts,
            "kind": "commit",
            "rows_added": rows_added,
            "rows_updated": rows_updated,
        }
    )


class TestClassify:
    def test_clean_record(self):
        assert classify(entries_received=5, ids_searched=5, ids_fetched=5, refused=False) == CLEAN

    def test_incomplete_extraction(self):
        # Fewer emails fetched than searched, but nothing staged from them
        # (entries_received <= ids_fetched), and the guard refused - still
        # flagged as incomplete since fewer emails were opened than searched.
        verdict = classify(entries_received=0, ids_searched=10, ids_fetched=3, refused=True)
        assert verdict == INCOMPLETE_EXTRACTION

    def test_fabrication_risk(self):
        # More entries received than emails fetched, but ids_fetched ==
        # ids_searched (no incomplete extraction) and refused is True, so the
        # guard caught it - flagged as fabrication risk, not guard-did-not-fire.
        verdict = classify(entries_received=10, ids_searched=5, ids_fetched=5, refused=True)
        assert verdict == FABRICATION_RISK

    def test_guard_did_not_fire(self):
        # Both drift signals present and refused is False - the guard should
        # have refused this but didn't. Most severe verdict.
        verdict = classify(entries_received=25, ids_searched=48, ids_fetched=5, refused=False)
        assert verdict == GUARD_DID_NOT_FIRE

    def test_guard_did_not_fire_takes_priority_when_both_flags_present(self):
        verdict = classify(entries_received=100, ids_searched=100, ids_fetched=1, refused=False)
        assert verdict == GUARD_DID_NOT_FIRE


class TestReadRecords:
    def test_clean_record_from_file(self, tmp_path):
        path = _write_log(tmp_path, [_record()])
        records, malformed = read_records(path)
        assert malformed == 0
        assert len(records) == 1
        assert records[0].verdict == CLEAN
        assert records[0].flagged is False
        assert records[0].severe is False

    def test_incomplete_extraction_from_file(self, tmp_path):
        path = _write_log(
            tmp_path,
            [_record(entries_received=0, ids_searched=10, ids_fetched=3, refused=True)],
        )
        records, malformed = read_records(path)
        assert malformed == 0
        assert records[0].verdict == INCOMPLETE_EXTRACTION
        assert records[0].flagged is True
        assert records[0].severe is False

    def test_fabrication_risk_from_file(self, tmp_path):
        path = _write_log(
            tmp_path,
            [_record(entries_received=10, ids_searched=5, ids_fetched=5, refused=True)],
        )
        records, malformed = read_records(path)
        assert malformed == 0
        assert records[0].verdict == FABRICATION_RISK
        assert records[0].flagged is True
        assert records[0].severe is False

    def test_guard_did_not_fire_from_file(self, tmp_path):
        # This mirrors the real historical record documented in check_drift's
        # module docstring: entries_received=25, ids_fetched=5, refused=False.
        path = _write_log(
            tmp_path,
            [_record(entries_received=25, ids_searched=48, ids_fetched=5, refused=False)],
        )
        records, malformed = read_records(path)
        assert malformed == 0
        assert records[0].verdict == GUARD_DID_NOT_FIRE
        assert records[0].flagged is True
        assert records[0].severe is True

    def test_malformed_line_is_skipped_and_counted(self, tmp_path):
        path = _write_log(
            tmp_path,
            [_record(), "{not valid json", _record(entries_received=99)],
        )
        records, malformed = read_records(path)
        assert malformed == 1
        assert len(records) == 2

    def test_line_missing_required_field_is_treated_as_malformed(self, tmp_path):
        truncated = json.dumps({"ts": "2026-08-01T00:00:00+00:00", "entries_received": 5})
        path = _write_log(tmp_path, [truncated])
        records, malformed = read_records(path)
        assert malformed == 1
        assert records == []

    def test_blank_lines_are_ignored_and_not_counted_as_malformed(self, tmp_path):
        path = _write_log(tmp_path, [_record(), "", _record(entries_received=2)])
        records, malformed = read_records(path)
        assert malformed == 0
        assert len(records) == 2

    def test_empty_file_returns_no_records(self, tmp_path):
        path = tmp_path / "run_log.jsonl"
        path.write_text("")
        records, malformed = read_records(str(path))
        assert records == []
        assert malformed == 0

    def test_missing_file_raises_file_not_found(self, tmp_path):
        missing_path = str(tmp_path / "does_not_exist.jsonl")
        try:
            read_records(missing_path)
            raised = False
        except FileNotFoundError:
            raised = True
        assert raised

    def test_stage_without_commit_is_reported_as_uncommitted(self, tmp_path):
        path = _write_log(tmp_path, [_record()])
        records, malformed = read_records(path)
        assert malformed == 0
        assert records[0].uncommitted is True
        assert records[0].display_verdict == STAGED_NO_COMMIT
        # Not folded into the count-based drift verdicts.
        assert records[0].verdict == CLEAN
        assert records[0].severe is False

    def test_stage_then_commit_is_quiet(self, tmp_path):
        path = _write_log(
            tmp_path,
            [
                _record(ts="2026-08-11T00:00:00+00:00"),
                _commit_record(ts="2026-08-11T00:01:00+00:00"),
            ],
        )
        records, malformed = read_records(path)
        assert malformed == 0
        assert len(records) == 1
        assert records[0].committed is True
        assert records[0].uncommitted is False
        assert records[0].display_verdict == CLEAN

    def test_refused_stage_without_commit_is_not_uncommitted(self, tmp_path):
        # An explicit refusal is the outcome - no commit is expected.
        path = _write_log(
            tmp_path,
            [_record(entries_received=0, ids_searched=10, ids_fetched=3, refused=True)],
        )
        records, _ = read_records(path)
        assert records[0].uncommitted is False
        assert records[0].display_verdict == INCOMPLETE_EXTRACTION

    def test_commit_pairs_with_most_recent_non_refused_stage(self, tmp_path):
        # stage A (ok), stage B (refused - never overwrote the pending plan),
        # commit: the commit applied A's plan, so A is the committed one.
        path = _write_log(
            tmp_path,
            [
                _record(ts="2026-08-11T00:00:00+00:00"),
                _record(
                    ts="2026-08-11T00:01:00+00:00",
                    entries_received=0,
                    ids_searched=10,
                    ids_fetched=3,
                    refused=True,
                ),
                _commit_record(ts="2026-08-11T00:02:00+00:00"),
            ],
        )
        records, _ = read_records(path)
        assert records[0].committed is True
        assert records[1].committed is False
        assert records[1].uncommitted is False

    def test_restaged_then_committed_flags_the_abandoned_stage(self, tmp_path):
        path = _write_log(
            tmp_path,
            [
                _record(ts="2026-08-11T00:00:00+00:00"),
                _record(ts="2026-08-11T00:01:00+00:00"),
                _commit_record(ts="2026-08-11T00:02:00+00:00"),
            ],
        )
        records, _ = read_records(path)
        assert records[0].uncommitted is True
        assert records[1].committed is True

    def test_uncommitted_annotation_composes_with_a_drift_verdict(self, tmp_path):
        path = _write_log(
            tmp_path,
            [_record(entries_received=25, ids_searched=48, ids_fetched=5, refused=False)],
        )
        records, _ = read_records(path)
        assert records[0].verdict == GUARD_DID_NOT_FIRE
        assert records[0].display_verdict == f"{GUARD_DID_NOT_FIRE} + {STAGED_NO_COMMIT}"

    def test_commit_records_are_not_returned_and_not_malformed(self, tmp_path):
        path = _write_log(tmp_path, [_commit_record()])
        records, malformed = read_records(path)
        assert records == []
        assert malformed == 0

    def test_truncated_commit_record_is_malformed(self, tmp_path):
        truncated = json.dumps({"ts": "2026-08-11T00:00:00+00:00", "kind": "commit"})
        path = _write_log(tmp_path, [truncated])
        records, malformed = read_records(path)
        assert records == []
        assert malformed == 1

    def test_existing_stage_records_still_parse_alongside_commit_records(self, tmp_path):
        # The historical stage shape (no "kind" field) must keep parsing
        # exactly as before in a log that also contains commit records.
        path = _write_log(
            tmp_path,
            [
                _record(entries_received=25, ids_searched=48, ids_fetched=5, refused=False),
                _record(ts="2026-08-11T00:00:00+00:00"),
                _commit_record(ts="2026-08-11T00:01:00+00:00"),
            ],
        )
        records, malformed = read_records(path)
        assert malformed == 0
        assert len(records) == 2
        assert records[0].verdict == GUARD_DID_NOT_FIRE
        assert records[1].verdict == CLEAN

    def test_last_n_returns_most_recent_records_only(self, tmp_path):
        path = _write_log(
            tmp_path,
            [
                _record(ts="2026-08-01T00:00:00+00:00", entries_received=1),
                _record(ts="2026-08-02T00:00:00+00:00", entries_received=2),
                _record(ts="2026-08-03T00:00:00+00:00", entries_received=3),
            ],
        )
        records, malformed = read_records(path, last=2)
        assert malformed == 0
        assert [r.entries_received for r in records] == [2, 3]
