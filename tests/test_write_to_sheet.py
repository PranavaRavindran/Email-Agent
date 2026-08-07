from unittest.mock import patch

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
