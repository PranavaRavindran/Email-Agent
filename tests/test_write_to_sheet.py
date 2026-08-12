import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import write_to_sheet as wts
from write_to_sheet import (
    _company_matches,
    _keys_match,
    _merge_duplicates,
    _normalize,
    _resolve_dates,
    _sort_key,
    _validate_entry,
    preview_resolve,
)


class _FakeToolContext:
    """Minimal stand-in for google.adk.tools.ToolContext.

    write_to_sheet only ever does state[key] = value, state.get(key), and
    `key in state` against tool_context.state, and a plain dict supports all
    three - so these tests never need to construct a real ADK State/Session.
    """

    def __init__(self):
        self.state = {}


# ---------------------------------------------------------------------------
# _normalize
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_lowercases(self):
        assert _normalize("ACME Corp") == _normalize("acme corp")
        assert _normalize("Acme") == "acme"

    def test_strips_parenthetical_content(self):
        assert _normalize("Delv ASSOC Software Eng (Princeton)") == "delv assoc software eng"

    def test_strips_trailing_legal_suffixes(self):
        a = _normalize("CrossLink Professional Tax Solutions LLC")
        b = _normalize("CrossLink Professional Tax Solutions")
        assert a == b

    def test_strips_after_first_comma_hyphen_or_en_dash(self):
        assert _normalize("AXS - Charlotte, NC") == "axs"
        assert _normalize("AXS, Charlotte NC") == "axs"
        assert _normalize("AXS – Charlotte") == "axs"

    def test_collapses_whitespace_and_removes_punctuation(self):
        assert _normalize("  Acme,   Inc!!  ") == "acme"
        assert _normalize("A.C.M.E.") == "acme"

    def test_regression_role_with_comma_qualifier_collides_with_plain_role(self):
        # Regression: "Software Engineer, Full Stack" normalizes to "software
        # engineer" because the trailing-segment strip fires on the comma,
        # which would collide with a plain "Software Engineer" role at the
        # same company. This is documented current behaviour, not desired
        # behaviour - it is a known limitation of the comma-stripping rule.
        assert _normalize("Software Engineer, Full Stack") == "software engineer"
        assert _normalize("Software Engineer, Full Stack") == _normalize("Software Engineer")


# ---------------------------------------------------------------------------
# _company_matches
# ---------------------------------------------------------------------------


class TestCompanyMatches:
    def test_exact_keys_match(self):
        assert _company_matches("acme", "acme") is True

    def test_prefix_match_on_whitespace_boundary(self):
        assert _company_matches("crosslink", "crosslink professional tax solutions") is True
        assert _company_matches("crosslink professional tax solutions", "crosslink") is True

    def test_prefix_must_break_on_whitespace_boundary(self):
        assert _company_matches("ibm", "ibmx") is False

    def test_empty_string_does_not_match_everything(self):
        assert _company_matches("", "acme") is False
        assert _company_matches("acme", "") is False
        assert _company_matches("", "") is True


# ---------------------------------------------------------------------------
# _keys_match
# ---------------------------------------------------------------------------


class TestKeysMatch:
    def test_matching_company_and_role(self):
        assert _keys_match("ibm", "software developer", "ibm", "software developer") is True

    def test_regression_role_must_match_exactly(self):
        # Regression: merging previously used a looser role comparison and
        # collapsed "software developer" with "software developer 2026 elh"
        # at the same company, silently dropping one of two distinct
        # applications from the sheet.
        assert (
            _keys_match("ibm", "software developer", "ibm", "software developer 2026 elh") is False
        )


# ---------------------------------------------------------------------------
# _validate_entry
# ---------------------------------------------------------------------------


def _entry(**overrides):
    base = {
        "company": "Acme Corp",
        "role": "Software Engineer",
        "date": "2026-06-04",
        "status": "Applied",
        "source": "Email",
    }
    base.update(overrides)
    return base


class TestValidateEntry:
    def test_accepts_well_formed_entry(self):
        cleaned, reason = _validate_entry(_entry())
        assert reason is None
        assert cleaned["company"] == "Acme Corp"
        assert cleaned["role"] == "Software Engineer"
        assert cleaned["date"] == "2026-06-04"
        assert cleaned["status"] == "Applied"

    def test_rejects_missing_company(self):
        _, reason = _validate_entry(_entry(company=None))
        assert reason == "missing or empty company"

    def test_rejects_empty_company(self):
        _, reason = _validate_entry(_entry(company="   "))
        assert reason == "missing or empty company"

    def test_rejects_missing_role(self):
        _, reason = _validate_entry(_entry(role=None))
        assert reason == "missing or empty role"

    def test_rejects_empty_role(self):
        _, reason = _validate_entry(_entry(role=""))
        assert reason == "missing or empty role"

    def test_rejects_missing_date(self):
        _, reason = _validate_entry(_entry(date=None))
        assert reason == "missing or empty date"

    def test_rejects_empty_date(self):
        _, reason = _validate_entry(_entry(date="  "))
        assert reason == "missing or empty date"

    def test_rejects_missing_status(self):
        _, reason = _validate_entry(_entry(status=None))
        assert reason == "missing or empty status"

    def test_rejects_empty_status(self):
        _, reason = _validate_entry(_entry(status=""))
        assert reason == "missing or empty status"

    def test_rejects_status_outside_permitted_values(self):
        _, reason = _validate_entry(_entry(status="Hold"))
        assert "not one of" in reason

    def test_accepts_and_corrects_case_whitespace_drift(self):
        cleaned, reason = _validate_entry(_entry(status=" rejected "))
        assert reason is None
        assert cleaned["status"] == "Rejected"

    def test_rejects_malformed_date(self):
        _, reason = _validate_entry(_entry(date="2026-13-45"))
        assert reason is not None

    def test_rejects_non_iso_date(self):
        _, reason = _validate_entry(_entry(date="July 13 2026"))
        assert reason is not None

    def test_rejects_company_starting_with_digit_run(self):
        _, reason = _validate_entry(_entry(company="123 Acme Corp"))
        assert "leading run of digits" in reason

    def test_rejects_role_starting_with_digit_run(self):
        _, reason = _validate_entry(_entry(role="456 Software Engineer"))
        assert "leading run of digits" in reason

    def test_rejects_truncated_marker_in_company(self):
        _, reason = _validate_entry(_entry(company="Acme Cor[truncated]"))
        assert "[truncated]" in reason

    def test_rejects_truncated_marker_in_role(self):
        _, reason = _validate_entry(_entry(role="Software Engin[truncated]"))
        assert "[truncated]" in reason

    def test_defaults_missing_source_to_email(self):
        entry = _entry()
        del entry["source"]
        cleaned, reason = _validate_entry(entry)
        assert reason is None
        assert cleaned["source"] == "Email"


# ---------------------------------------------------------------------------
# _merge_duplicates
# ---------------------------------------------------------------------------


class TestMergeDuplicates:
    def test_regression_abbott_earliest_date_latest_status(self):
        # Regression: the Abbott application had an "Applied" entry dated
        # earlier than its "Rejected" entry; dedup previously kept whichever
        # entry sorted last and could drop the later rejection status,
        # reverting a rejected application back to Applied.
        applied = _entry(company="Abbott", role="Data Analyst", date="2026-06-04", status="Applied")
        rejected = _entry(
            company="Abbott", role="Data Analyst", date="2026-07-13", status="Rejected"
        )

        deduped, duplicates = _merge_duplicates([applied, rejected])

        assert len(deduped) == 1
        assert deduped[0]["date"] == "2026-06-04"
        assert deduped[0]["status"] == "Rejected"
        assert len(duplicates) == 1

    def test_merge_keeps_longest_company_and_role_strings(self):
        # "Acme Corp" and "Software Engineer (Remote)" normalize to the same
        # keys as "Acme" and "Software Engineer" (legal-suffix and
        # parenthetical stripping), so these two entries are one group.
        short = _entry(
            company="Acme", role="Software Engineer", date="2026-06-01", status="Applied"
        )
        long = _entry(
            company="Acme Corp",
            role="Software Engineer (Remote)",
            date="2026-06-02",
            status="Applied",
        )

        deduped, _ = _merge_duplicates([short, long])

        assert len(deduped) == 1
        assert deduped[0]["company"] == "Acme Corp"
        assert deduped[0]["role"] == "Software Engineer (Remote)"

    def test_three_entries_for_same_application_merge_to_one(self):
        e1 = _entry(company="Acme", role="Eng", date="2026-06-01", status="Applied")
        e2 = _entry(company="Acme", role="Eng", date="2026-06-02", status="Interviewing")
        e3 = _entry(company="Acme", role="Eng", date="2026-06-03", status="Rejected")

        deduped, duplicates = _merge_duplicates([e1, e2, e3])

        assert len(deduped) == 1
        assert deduped[0]["date"] == "2026-06-01"
        assert deduped[0]["status"] == "Rejected"
        assert len(duplicates) == 1

    def test_different_roles_at_same_company_not_merged(self):
        e1 = _entry(company="Acme", role="Software Engineer", date="2026-06-01", status="Applied")
        e2 = _entry(company="Acme", role="Product Manager", date="2026-06-02", status="Applied")

        deduped, duplicates = _merge_duplicates([e1, e2])

        assert len(deduped) == 2
        assert duplicates == []

    def test_duplicates_in_batch_one_record_per_merged_group(self):
        e1 = _entry(company="Acme", role="Eng", date="2026-06-01", status="Applied")
        e2 = _entry(company="Acme", role="Eng", date="2026-06-02", status="Rejected")
        e3 = _entry(company="Other Co", role="PM", date="2026-06-01", status="Applied")
        e4 = _entry(company="Other Co", role="PM", date="2026-06-02", status="Interviewing")

        deduped, duplicates = _merge_duplicates([e1, e2, e3, e4])

        assert len(deduped) == 2
        assert len(duplicates) == 2

    def test_duplicates_in_batch_empty_when_no_duplicates(self):
        e1 = _entry(company="Acme", role="Eng", date="2026-06-01", status="Applied")
        e2 = _entry(company="Other Co", role="PM", date="2026-06-02", status="Applied")

        deduped, duplicates = _merge_duplicates([e1, e2])

        assert len(deduped) == 2
        assert duplicates == []

    def test_regression_truncated_company_form_merges_with_full_form(self):
        # Regression: the agent emitted the short company form
        # ("CrossLink") for one email and the full legal form for another,
        # for the identical role. _merge_duplicates previously grouped on
        # an exact normalized key only, so these sailed through as two
        # separate new rows instead of merging like sheet matching
        # (_keys_match) would have caught.
        short = _entry(
            company="CrossLink", role="Software Engineer I", date="2026-06-01", status="Applied"
        )
        long = _entry(
            company="CrossLink Professional Tax Solutions",
            role="Software Engineer I",
            date="2026-06-02",
            status="Applied",
        )

        deduped, duplicates = _merge_duplicates([short, long])

        assert len(deduped) == 1
        assert deduped[0]["company"] == "CrossLink Professional Tax Solutions"
        assert deduped[0]["role"] == "Software Engineer I"
        assert len(duplicates) == 1

    def test_company_prefix_must_break_on_whitespace_boundary_not_merged(self):
        ibm = _entry(company="IBM", role="Software Engineer", date="2026-06-01", status="Applied")
        ibmx = _entry(company="IBMX", role="Software Engineer", date="2026-06-02", status="Applied")

        deduped, duplicates = _merge_duplicates([ibm, ibmx])

        assert len(deduped) == 2
        assert duplicates == []

    def test_entry_order_otherwise_preserved(self):
        e1 = _entry(company="Zeta", role="Eng", date="2026-06-01", status="Applied")
        e2 = _entry(company="Acme", role="PM", date="2026-06-02", status="Applied")
        e3 = _entry(company="Middle", role="Eng", date="2026-06-03", status="Applied")

        deduped, _ = _merge_duplicates([e1, e2, e3])

        assert [d["company"] for d in deduped] == ["Zeta", "Acme", "Middle"]


# ---------------------------------------------------------------------------
# _sort_key
# ---------------------------------------------------------------------------


class TestSortKey:
    def test_entries_sort_oldest_first(self):
        entries = [
            _entry(company="C", date="2026-06-03"),
            _entry(company="A", date="2026-06-01"),
            _entry(company="B", date="2026-06-02"),
        ]
        entries.sort(key=_sort_key)
        assert [e["company"] for e in entries] == ["A", "B", "C"]

    def test_missing_or_unparseable_date_sorts_last(self):
        good = _entry(company="Good", date="2026-06-01")
        missing_date = dict(good)
        del missing_date["date"]
        missing_date["company"] = "Missing"
        bad_date = _entry(company="Bad", date="not-a-date")

        entries = [bad_date, missing_date, good]
        entries.sort(key=_sort_key)

        assert entries[0]["company"] == "Good"
        assert {entries[1]["company"], entries[2]["company"]} == {"Missing", "Bad"}


# ---------------------------------------------------------------------------
# _resolve_dates
# ---------------------------------------------------------------------------


class TestResolveDates:
    @patch("write_to_sheet.is_confirmation_email")
    @patch("write_to_sheet.get_email_detail")
    @patch("write_to_sheet.find_application_date")
    def test_observed_confirmation_skips_lookback(
        self, mock_find, mock_get_detail, mock_is_confirmation
    ):
        mock_get_detail.return_value = {"email": {"subject": "s", "body": "b"}}
        mock_is_confirmation.return_value = True
        entry = _entry(source_email_ids=["id-1"])

        resolved = _resolve_dates([entry])

        mock_find.assert_not_called()
        assert resolved[0]["date"] == entry["date"]

    @patch("write_to_sheet.is_confirmation_email")
    @patch("write_to_sheet.get_email_detail")
    @patch("write_to_sheet.find_application_date")
    def test_no_observed_confirmation_runs_lookback(
        self, mock_find, mock_get_detail, mock_is_confirmation
    ):
        mock_get_detail.return_value = {"email": {"subject": "s", "body": "b"}}
        mock_is_confirmation.return_value = False
        mock_find.return_value = {"found": True, "date": "2026-05-01", "email_id": "abc"}
        entry = _entry(source_email_ids=["id-1"], date="2026-06-04")

        resolved = _resolve_dates([entry])

        mock_find.assert_called_once()
        assert resolved[0]["date"] == "2026-05-01"
        assert resolved[0]["date_approximate"] is False

    @patch("write_to_sheet.is_confirmation_email")
    @patch("write_to_sheet.get_email_detail")
    @patch("write_to_sheet.find_application_date")
    def test_lookback_passes_source_text_from_the_entrys_own_group(
        self, mock_find, mock_get_detail, mock_is_confirmation
    ):
        # The group's own source email (the status update that triggered
        # this lookback) is passed through as source_text so
        # find_application_date can extract a requisition/job identifier
        # from it and reject a weak match belonging to a different
        # requisition at the same company (Fix 1/Fix 4). It must be reused
        # from the fetch _has_observed_confirmation already did, not
        # fetched a second time.
        mock_get_detail.return_value = {
            "email": {"subject": "Update on req 203991", "body": "Requisition Number: 203991"}
        }
        mock_is_confirmation.return_value = False
        mock_find.return_value = {"found": True, "date": "2026-05-01", "email_id": "abc"}
        entry = _entry(source_email_ids=["id-1"], date="2026-06-04")

        _resolve_dates([entry])

        mock_find.assert_called_once_with(
            company=entry["company"],
            role=entry["role"],
            before_date=entry["date"],
            source_text="Update on req 203991 Requisition Number: 203991",
        )
        mock_get_detail.assert_called_once_with("id-1")

    @patch("write_to_sheet.find_application_date")
    def test_lookback_not_found_keeps_date_and_flags_approximate(self, mock_find):
        mock_find.return_value = {"found": False, "date": "", "email_id": ""}
        entry = _entry(source_email_ids=[], date="2026-06-04")

        resolved = _resolve_dates([entry])

        assert resolved[0]["date"] == "2026-06-04"
        assert resolved[0]["date_approximate"] is True

    @patch("write_to_sheet.find_application_date")
    def test_lookback_exception_leaves_entry_unchanged_and_approximate(self, mock_find):
        mock_find.side_effect = RuntimeError("boom")
        entry = _entry(source_email_ids=[], date="2026-06-04")

        resolved = _resolve_dates([entry])

        assert resolved[0]["date"] == "2026-06-04"
        assert resolved[0]["date_approximate"] is True

    @patch("write_to_sheet.find_application_date")
    def test_exception_on_one_entry_in_a_batch_does_not_drop_the_others(self, mock_find):
        # Regression: a transient error (e.g. a 503 mid-scan) during one
        # entry's lookback must never cause that entry - or any other entry
        # in the same batch - to vanish from the result. The failing entry
        # should still appear, flagged date_approximate True.
        def side_effect(company, role, before_date, source_text):
            if company == "CrossLink":
                raise RuntimeError("503 UNAVAILABLE")
            return {"found": True, "date": "2026-05-01", "email_id": "abc"}

        mock_find.side_effect = side_effect
        entries = [
            _entry(company="Acme Corp", source_email_ids=[], date="2026-06-01"),
            _entry(company="CrossLink", source_email_ids=[], date="2026-06-02"),
            _entry(company="SAIC", source_email_ids=[], date="2026-06-03"),
        ]

        resolved = _resolve_dates(entries)

        assert len(resolved) == 3
        assert {entry["company"] for entry in resolved} == {"Acme Corp", "CrossLink", "SAIC"}

        by_company = {entry["company"]: entry for entry in resolved}
        assert by_company["CrossLink"]["date"] == "2026-06-02"
        assert by_company["CrossLink"]["date_approximate"] is True
        assert by_company["Acme Corp"]["date_approximate"] is False
        assert by_company["SAIC"]["date_approximate"] is False

    @patch("write_to_sheet.find_application_date")
    def test_missing_source_email_ids_key_treated_as_no_confirmation(self, mock_find):
        mock_find.return_value = {"found": True, "date": "2026-05-01", "email_id": "abc"}
        entry = _entry()
        assert "source_email_ids" not in entry

        resolved = _resolve_dates([entry])

        mock_find.assert_called_once()
        assert resolved[0]["date"] == "2026-05-01"
        assert resolved[0]["date_approximate"] is False


# ---------------------------------------------------------------------------
# preview_resolve - fetch-completeness guard
# ---------------------------------------------------------------------------


class TestPreviewResolveFetchCompleteness:
    @patch("write_to_sheet.get_fetched_ids")
    @patch("write_to_sheet.get_last_search_count")
    def test_refuses_when_fetched_count_less_than_searched_count(self, mock_searched, mock_fetched):
        mock_searched.return_value = 51
        mock_fetched.return_value = {f"id-{i}" for i in range(16)}

        result = preview_resolve([_entry()])

        assert "error" in result
        assert "16" in result["error"]
        assert "51" in result["error"]
        assert "entries" not in result

    @patch("write_to_sheet._resolve_dates")
    @patch("write_to_sheet.get_fetched_ids")
    @patch("write_to_sheet.get_last_search_count")
    def test_does_not_resolve_or_merge_on_refusal(
        self, mock_searched, mock_fetched, mock_resolve_dates
    ):
        mock_searched.return_value = 10
        mock_fetched.return_value = {"id-1", "id-2"}

        preview_resolve([_entry()])

        mock_resolve_dates.assert_not_called()

    @patch("write_to_sheet.find_application_date")
    @patch("write_to_sheet.get_fetched_ids")
    @patch("write_to_sheet.get_last_search_count")
    def test_proceeds_when_fetched_count_meets_searched_count(
        self, mock_searched, mock_fetched, mock_find
    ):
        mock_searched.return_value = 1
        mock_fetched.return_value = {"id-1"}
        mock_find.return_value = {"found": False, "date": "", "email_id": ""}

        result = preview_resolve([_entry(source_email_ids=[])])

        assert "error" not in result
        assert "entries" in result

    @patch("write_to_sheet.find_application_date")
    @patch("write_to_sheet.get_fetched_ids")
    @patch("write_to_sheet.get_last_search_count")
    def test_no_search_yet_does_not_refuse(self, mock_searched, mock_fetched, mock_find):
        mock_searched.return_value = 0
        mock_fetched.return_value = set()
        mock_find.return_value = {"found": False, "date": "", "email_id": ""}

        result = preview_resolve([_entry(source_email_ids=[])])

        assert "error" not in result
        assert "entries" in result


# ---------------------------------------------------------------------------
# _read_existing_rows - raw API path and MCP path (USE_MCP_SHEETS)
# ---------------------------------------------------------------------------


class TestReadExistingRows:
    def test_mcp_path_maps_get_range_response_to_values(self, monkeypatch):
        monkeypatch.setenv("USE_MCP_SHEETS", "1")
        # Representative sheets_getRange response, per SheetsService.ts's
        # getRange handler - see MCP_INTEGRATION.md gate G3: {range, values}
        # wraps the exact same raw values.get() response shape the raw-API
        # path below already consumes.
        fixture = {
            "range": "Tracker!A3:F5",
            "values": [
                ["2026-06-01", "Acme Corp", "Software Engineer", "", "Email", "Applied"],
                ["", "Other Co", "Product Manager", "", "Email", "Rejected"],
            ],
        }
        calls = []

        def fake_mcp_call(tool_name, arguments):
            calls.append((tool_name, arguments))
            return fixture

        monkeypatch.setattr(wts, "mcp_call", fake_mcp_call)

        rows = wts._read_existing_rows(sheets_service=None)

        assert calls == [
            ("sheets_getRange", {"spreadsheetId": wts._SPREADSHEET_ID, "range": wts._READ_RANGE})
        ]
        assert rows == fixture["values"]

    def test_raw_path_reads_via_sheets_service(self, monkeypatch):
        monkeypatch.setenv("USE_MCP_SHEETS", "0")

        class _FakeValues:
            def get(self, spreadsheetId, range):
                assert spreadsheetId == wts._SPREADSHEET_ID
                assert range == wts._READ_RANGE
                return self

            def execute(self):
                return {"values": [["2026-06-01", "Acme", "Eng", "", "Email", "Applied"]]}

        class _FakeSpreadsheets:
            def values(self):
                return _FakeValues()

        class _FakeSheetsService:
            def spreadsheets(self):
                return _FakeSpreadsheets()

        rows = wts._read_existing_rows(_FakeSheetsService())

        assert rows == [["2026-06-01", "Acme", "Eng", "", "Email", "Applied"]]


# ---------------------------------------------------------------------------
# stage_write / commit_write - ADK session-state seam (Fix 1)
#
# The pending write used to persist to pending_write.json; it now persists to
# tool_context.state[_PENDING_WRITE_STATE_KEY]. There were no pre-existing
# tests exercising stage_write/commit_write's persistence directly (only the
# lower-level helpers above were covered), so this section is new coverage
# for that seam rather than an adaptation of prior assertions.
# ---------------------------------------------------------------------------


class TestStageWriteSessionState:
    def test_empty_entries_returns_error_and_does_not_touch_state(self):
        tool_context = _FakeToolContext()

        result = wts.stage_write([], tool_context)

        assert "error" in result
        assert wts._PENDING_WRITE_STATE_KEY not in tool_context.state

    @patch("write_to_sheet._resolve")
    @patch("write_to_sheet._merge_duplicates")
    @patch("write_to_sheet._resolve_dates")
    @patch("write_to_sheet.get_last_search_range")
    @patch("write_to_sheet.get_fetched_ids")
    @patch("write_to_sheet.get_last_search_count")
    def test_stores_pending_payload_under_the_state_key(
        self,
        mock_searched,
        mock_fetched,
        mock_range,
        mock_resolve_dates,
        mock_merge,
        mock_resolve,
    ):
        mock_searched.return_value = 0
        mock_fetched.return_value = set()
        mock_range.return_value = ("", "")
        mock_resolve_dates.side_effect = lambda entries: entries
        mock_merge.side_effect = lambda entries: (entries, [])
        mock_resolve.return_value = {
            "sheets_service": None,
            "entries": [_entry()],
            "updates": [{"range": "Tracker!F3", "values": [["Rejected"]]}],
            "rows_to_append": [],
            "last_data_row": 3,
            "new_rows": [],
            "status_changes": [
                {
                    "company": "Acme Corp",
                    "role": "Software Engineer",
                    "old_status": "Applied",
                    "new_status": "Rejected",
                }
            ],
            "unchanged_count": 0,
            "unseen_rows": [],
        }
        tool_context = _FakeToolContext()

        result = wts.stage_write([_entry()], tool_context)

        assert result["has_changes"] is True
        pending = tool_context.state[wts._PENDING_WRITE_STATE_KEY]
        assert pending["updates"] == [{"range": "Tracker!F3", "values": [["Rejected"]]}]
        assert pending["rows_to_append"] == []
        assert pending["last_data_row"] == 3
        assert "staged_at" in pending


class TestCommitWriteSessionState:
    def test_no_pending_state_returns_error(self):
        tool_context = _FakeToolContext()

        result = wts.commit_write(tool_context)

        assert result == {"error": "No pending write found. Call stage_write before commit_write."}

    def test_cleared_pending_state_is_treated_as_absent(self):
        # After a prior commit, the key is set to None (State has no delete) -
        # this must be rejected exactly like a key that was never staged.
        tool_context = _FakeToolContext()
        tool_context.state[wts._PENDING_WRITE_STATE_KEY] = None

        result = wts.commit_write(tool_context)

        assert result == {"error": "No pending write found. Call stage_write before commit_write."}

    def test_stale_pending_state_is_rejected_exactly_as_the_file_was(self):
        tool_context = _FakeToolContext()
        stale_staged_at = datetime.now(UTC) - timedelta(hours=1, minutes=1)
        tool_context.state[wts._PENDING_WRITE_STATE_KEY] = {
            "entries": [],
            "updates": [],
            "rows_to_append": [],
            "last_data_row": 2,
            "staged_at": stale_staged_at.isoformat(),
            "diff": {},
        }

        result = wts.commit_write(tool_context)

        assert result == {
            "error": "The pending write is stale (staged more than 1 hour ago) "
            "and should be re-staged."
        }
        # A stale rejection must not clear the state - re-staging overwrites it.
        assert tool_context.state[wts._PENDING_WRITE_STATE_KEY] is not None

    @patch("write_to_sheet._apply")
    @patch("write_to_sheet.build")
    @patch("write_to_sheet.get_gmail_service")
    def test_applies_pending_state_and_clears_it_after(
        self, mock_get_service, mock_build, mock_apply, monkeypatch, tmp_path
    ):
        # Redirect the run log: commit_write now appends a commit record,
        # which must not land in the repo's real run_log.jsonl.
        monkeypatch.setattr(wts, "_RUN_LOG_PATH", str(tmp_path / "run_log.jsonl"))
        mock_apply.return_value = {"rows_added": 1, "rows_updated": 1}
        tool_context = _FakeToolContext()
        pending = {
            "entries": [_entry()],
            "updates": [{"range": "Tracker!F3", "values": [["Rejected"]]}],
            "rows_to_append": [["2026-06-01", "Acme", "Eng", "", "Email", "Applied"]],
            "last_data_row": 4,
            "staged_at": datetime.now(UTC).isoformat(),
            "diff": {},
        }
        tool_context.state[wts._PENDING_WRITE_STATE_KEY] = pending

        result = wts.commit_write(tool_context)

        assert result == {"rows_added": 1, "rows_updated": 1}
        mock_apply.assert_called_once_with(
            mock_build.return_value,
            pending["updates"],
            pending["rows_to_append"],
            pending["last_data_row"],
        )
        assert tool_context.state[wts._PENDING_WRITE_STATE_KEY] is None


class TestCommitWriteLogging:
    def _staged_context(self):
        tool_context = _FakeToolContext()
        tool_context.state[wts._PENDING_WRITE_STATE_KEY] = {
            "entries": [_entry()],
            "updates": [],
            "rows_to_append": [["2026-06-01", "Acme", "Eng", "", "Email", "Applied"]],
            "last_data_row": 4,
            "staged_at": datetime.now(UTC).isoformat(),
            "diff": {},
        }
        return tool_context

    @patch("write_to_sheet._apply")
    @patch("write_to_sheet.build")
    @patch("write_to_sheet.get_gmail_service")
    def test_commit_appends_a_commit_record_with_outcome_fields(
        self, mock_get_service, mock_build, mock_apply, monkeypatch, tmp_path
    ):
        log_path = tmp_path / "run_log.jsonl"
        monkeypatch.setattr(wts, "_RUN_LOG_PATH", str(log_path))
        mock_apply.return_value = {"rows_added": 3, "rows_updated": 2}

        wts.commit_write(self._staged_context())

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["kind"] == "commit"
        assert record["rows_added"] == 3
        assert record["rows_updated"] == 2
        assert "ts" in record

    @patch("write_to_sheet._apply")
    @patch("write_to_sheet.build")
    @patch("write_to_sheet.get_gmail_service")
    def test_logging_failure_does_not_affect_commit_result(
        self, mock_get_service, mock_build, mock_apply, monkeypatch, tmp_path
    ):
        # Same never-raises contract as _log_run: point the log at an
        # unwritable path and the commit must still succeed.
        monkeypatch.setattr(wts, "_RUN_LOG_PATH", str(tmp_path / "no-such-dir" / "run.jsonl"))
        mock_apply.return_value = {"rows_added": 1, "rows_updated": 0}

        result = wts.commit_write(self._staged_context())

        assert result == {"rows_added": 1, "rows_updated": 0}
