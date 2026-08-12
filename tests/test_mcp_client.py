"""Tests for tools/mcp_client.py's parser layer and dispatch against the
google_workspace_mcp server's FORMATTED-TEXT responses.

The transport is stubbed at mcp_client._call_tool_text - the seam between
"call the server, get text back" and "parse text into the dict callers
expect" - so no test opens a network connection or event loop. The batch
and search sample texts are VERBATIM captures from the deployed Cloud Run
service (2026-08-11); if the server's format drifts, these tests are the
spec for what the parsers must accept.
"""

import pytest

import tools.mcp_client as mc

_EMAIL = "user@example.com"


@pytest.fixture(autouse=True)
def _mcp_env(monkeypatch):
    monkeypatch.setenv("USER_GOOGLE_EMAIL", _EMAIL)


def _stub_text(monkeypatch, text):
    """Stubs the transport to return `text` and records (tool, args) calls."""
    calls = []

    def fake_call_tool_text(tool_name, arguments):
        calls.append((tool_name, arguments))
        return text

    monkeypatch.setattr(mc, "_call_tool_text", fake_call_tool_text)
    return calls


# ---------------------------------------------------------------------------
# get_gmail_messages_content_batch
# ---------------------------------------------------------------------------

# Verbatim capture from the deployed service (format=full), 2026-08-11.
_BATCH_SAMPLE = (
    "Retrieved 1 messages:\n"
    "\n"
    "Message ID: 19f901ea7b186afc\n"
    "Subject: Entry-level Talent Recruiting - Cisco Application Status Update\n"
    "From: Cisco@myworkday.com\n"
    "Date: Thu, 23 Jul 2026 17:55:55 +0000\n"
    "Message-ID: <0101019f901ea5d1-...@us-west-2.amazonses.com>\n"
    "To: pranava.ravindran19@gmail.com\n"
    "Cc: [not present in Gmail response]\n"
    "List-Unsubscribe: <https://...>\n"
    "Web Link: https://mail.google.com/mail/u/0/#all/19f901ea7b186afc\n"
    "\n"
    "--- BODY ---\n"
    "Hello Pranava, Thank you for applying to the Software Engineer I...\n"
)


def _batch_record(mid, body=None):
    """A per-message record in the server's format=full batch layout."""
    if body is None:
        body = f"body of {mid}"
    return (
        f"Message ID: {mid}\n"
        f"Subject: subject {mid}\n"
        f"From: from@example.com\n"
        f"Date: Thu, 23 Jul 2026 17:55:55 +0000\n"
        f"To: to@example.com\n"
        f"Cc: [not present in Gmail response]\n"
        f"Web Link: https://mail.google.com/mail/u/0/#all/{mid}\n"
        f"\n"
        f"--- BODY ---\n"
        f"{body}\n"
    )


def _batch_text(records):
    return f"Retrieved {len(records)} messages:\n\n" + "\n---\n\n".join(records)


class TestBatchParser:
    def test_verbatim_sample_parses_to_expected_dict(self, monkeypatch):
        _stub_text(monkeypatch, _BATCH_SAMPLE)

        result = mc.mcp_call(
            "get_gmail_messages_content_batch", {"message_ids": ["19f901ea7b186afc"]}
        )

        assert result == {
            "messages": [
                {
                    "id": "19f901ea7b186afc",
                    "subject": "Entry-level Talent Recruiting - Cisco Application Status Update",
                    "from": "Cisco@myworkday.com",
                    "date": "Thu, 23 Jul 2026 17:55:55 +0000",
                    "to": "pranava.ravindran19@gmail.com",
                    "web_link": "https://mail.google.com/mail/u/0/#all/19f901ea7b186afc",
                    "body": "Hello Pranava, Thank you for applying to the Software Engineer I...",
                }
            ]
        }

    def test_cc_sentinel_maps_to_absent_key_not_literal(self, monkeypatch):
        _stub_text(monkeypatch, _BATCH_SAMPLE)

        result = mc.mcp_call(
            "get_gmail_messages_content_batch", {"message_ids": ["19f901ea7b186afc"]}
        )

        assert "cc" not in result["messages"][0]

    def test_header_values_containing_colons_survive(self, monkeypatch):
        _stub_text(monkeypatch, _BATCH_SAMPLE)

        message = mc.mcp_call(
            "get_gmail_messages_content_batch", {"message_ids": ["19f901ea7b186afc"]}
        )["messages"][0]

        assert message["web_link"] == "https://mail.google.com/mail/u/0/#all/19f901ea7b186afc"
        assert message["date"] == "Thu, 23 Jul 2026 17:55:55 +0000"

    def test_body_containing_literal_body_delimiter_parses(self, monkeypatch):
        body = "before\n--- BODY ---\nafter"
        _stub_text(monkeypatch, _batch_text([_batch_record("id-1", body=body)]))

        result = mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ["id-1"]})

        assert result["messages"][0]["body"] == body

    def test_multiple_records_parse_in_request_order(self, monkeypatch):
        _stub_text(monkeypatch, _batch_text([_batch_record("id-1"), _batch_record("id-2")]))

        result = mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ["id-1", "id-2"]})

        assert [m["id"] for m in result["messages"]] == ["id-1", "id-2"]
        assert result["messages"][1]["body"] == "body of id-2"

    def test_per_message_error_record_becomes_error_entry(self, monkeypatch):
        records = [_batch_record("id-1"), "⚠️ Message id-2: HttpError 404: not found\n"]
        _stub_text(monkeypatch, _batch_text(records))

        result = mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ["id-1", "id-2"]})

        assert result["messages"][1] == {"id": "id-2", "error": "HttpError 404: not found"}

    def test_malformed_text_raises_with_snippet(self, monkeypatch):
        _stub_text(monkeypatch, "Totally unexpected response body")

        with pytest.raises(mc.MCPClientError, match="Totally unexpected response body"):
            mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ["id-1"]})

    def test_count_mismatch_raises_instead_of_partial_parse(self, monkeypatch):
        # Reports 2 but only 1 requested id: never silently return a subset.
        _stub_text(
            monkeypatch,
            _batch_text([_batch_record("id-1"), _batch_record("id-2")]),
        )

        with pytest.raises(mc.MCPClientError, match="requested"):
            mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ["id-1"]})

    def test_over_25_ids_chunk_into_multiple_calls_and_merge(self, monkeypatch):
        ids = [f"id-{i}" for i in range(51)]
        calls = []

        def fake_call_tool_text(tool_name, arguments):
            calls.append((tool_name, arguments))
            return _batch_text([_batch_record(mid) for mid in arguments["message_ids"]])

        monkeypatch.setattr(mc, "_call_tool_text", fake_call_tool_text)

        result = mc.mcp_call("get_gmail_messages_content_batch", {"message_ids": ids})

        assert [len(args["message_ids"]) for _, args in calls] == [25, 25, 1]
        assert all(tool == "get_gmail_messages_content_batch" for tool, _ in calls)
        assert all(args["user_google_email"] == _EMAIL for _, args in calls)
        assert [m["id"] for m in result["messages"]] == ids


# ---------------------------------------------------------------------------
# gmail_get -> get_gmail_message_content
# ---------------------------------------------------------------------------

# The single-message layout: same header block minus the "Message ID"/"Web
# Link" lines, blank line, then the body (no trailing newline appended).
_SINGLE_SAMPLE = (
    "Subject: Entry-level Talent Recruiting - Cisco Application Status Update\n"
    "From: Cisco@myworkday.com\n"
    "Date: Thu, 23 Jul 2026 17:55:55 +0000\n"
    "Message-ID: <0101019f901ea5d1-...@us-west-2.amazonses.com>\n"
    "To: pranava.ravindran19@gmail.com\n"
    "Cc: [not present in Gmail response]\n"
    "List-Unsubscribe: <https://...>\n"
    "\n"
    "--- BODY ---\n"
    "Hello Pranava, Thank you for applying to the Software Engineer I..."
)


class TestGmailGet:
    def test_translates_to_new_tool_name_and_arguments(self, monkeypatch):
        calls = _stub_text(monkeypatch, _SINGLE_SAMPLE)

        mc.mcp_call("gmail_get", {"messageId": "19f901ea7b186afc"})

        assert calls == [
            (
                "get_gmail_message_content",
                {"message_id": "19f901ea7b186afc", "user_google_email": _EMAIL},
            )
        ]

    def test_parses_to_the_shape_get_email_detail_expects(self, monkeypatch):
        _stub_text(monkeypatch, _SINGLE_SAMPLE)

        result = mc.mcp_call("gmail_get", {"messageId": "19f901ea7b186afc"})

        assert result == {
            "subject": "Entry-level Talent Recruiting - Cisco Application Status Update",
            "from": "Cisco@myworkday.com",
            "date": "Thu, 23 Jul 2026 17:55:55 +0000",
            "to": "pranava.ravindran19@gmail.com",
            "body": "Hello Pranava, Thank you for applying to the Software Engineer I...",
        }

    def test_empty_body_placeholder_maps_to_empty_string(self, monkeypatch):
        text = _SINGLE_SAMPLE.split("--- BODY ---")[0] + "--- BODY ---\n[No text/plain body found]"
        _stub_text(monkeypatch, text)

        result = mc.mcp_call("gmail_get", {"messageId": "x"})

        assert result["body"] == ""

    def test_attachments_section_is_stripped_from_body(self, monkeypatch):
        text = _SINGLE_SAMPLE + (
            "\n\n--- ATTACHMENTS ---\n"
            "1. resume.pdf (application/pdf, 120.0 KB)\n"
            "   Attachment ID: att-1\n"
            "   Use get_gmail_attachment_content(message_id='x', attachment_id='att-1') to download"
        )
        _stub_text(monkeypatch, text)

        result = mc.mcp_call("gmail_get", {"messageId": "x"})

        assert (
            result["body"] == "Hello Pranava, Thank you for applying to the Software Engineer I..."
        )

    def test_body_containing_literal_delimiter_splits_on_first_only(self, monkeypatch):
        body = "quoted text:\n--- BODY ---\nstill the body"
        text = _SINGLE_SAMPLE.split("--- BODY ---")[0] + "--- BODY ---\n" + body
        _stub_text(monkeypatch, text)

        result = mc.mcp_call("gmail_get", {"messageId": "x"})

        assert result["body"] == body

    def test_missing_body_delimiter_raises_with_snippet(self, monkeypatch):
        _stub_text(monkeypatch, "<html><body>an error page, not a message</body></html>")

        with pytest.raises(mc.MCPClientError, match="an error page"):
            mc.mcp_call("gmail_get", {"messageId": "x"})


# ---------------------------------------------------------------------------
# search_gmail_messages
# ---------------------------------------------------------------------------

# Verbatim capture from the deployed service, 2026-08-11.
_SEARCH_SAMPLE = (
    "Found 1 messages matching 'from:cisco':\n"
    "\n"
    "📧 MESSAGES:\n"
    "  1. Message ID: 19f901ea7b186afc\n"
    "     Web Link: https://mail.google.com/mail/u/0/#all/19f901ea7b186afc\n"
    "     Thread ID: 19f901ea7b186afc\n"
    "     Thread Link: https://mail.google.com/mail/u/0/#all/19f901ea7b186afc\n"
    "\n"
    "💡 USAGE:\n"
    "  • Pass the Message IDs **as a list** to get_gmail_messages_content_batch()\n"
    "\n"
    "📄 PAGINATION: To get the next page, call search_gmail_messages again with page_token='07277724609091224123'"
)


class TestSearchParser:
    def test_verbatim_sample_parses_to_expected_dict(self, monkeypatch):
        calls = _stub_text(monkeypatch, _SEARCH_SAMPLE)

        result = mc.mcp_call("search_gmail_messages", {"query": "from:cisco"})

        assert calls == [
            ("search_gmail_messages", {"query": "from:cisco", "user_google_email": _EMAIL})
        ]
        assert result == {
            "messages": [
                {
                    "id": "19f901ea7b186afc",
                    "web_link": "https://mail.google.com/mail/u/0/#all/19f901ea7b186afc",
                    "thread_id": "19f901ea7b186afc",
                    "thread_link": "https://mail.google.com/mail/u/0/#all/19f901ea7b186afc",
                }
            ],
            "next_page_token": "07277724609091224123",
        }

    def test_no_results_text_maps_to_empty_list(self, monkeypatch):
        _stub_text(monkeypatch, "No messages found for query: 'from:nobody'")

        result = mc.mcp_call("search_gmail_messages", {"query": "from:nobody"})

        assert result == {"messages": [], "next_page_token": None}

    def test_no_pagination_section_means_no_token(self, monkeypatch):
        text = _SEARCH_SAMPLE.split("\n\n📄 PAGINATION")[0]
        _stub_text(monkeypatch, text)

        result = mc.mcp_call("search_gmail_messages", {"query": "from:cisco"})

        assert result["next_page_token"] is None

    def test_entry_count_mismatch_raises(self, monkeypatch):
        text = _SEARCH_SAMPLE.replace("Found 1 messages", "Found 2 messages")
        _stub_text(monkeypatch, text)

        with pytest.raises(mc.MCPClientError, match="reports 2"):
            mc.mcp_call("search_gmail_messages", {"query": "from:cisco"})

    def test_malformed_text_raises_with_snippet(self, monkeypatch):
        _stub_text(monkeypatch, "Internal proxy error 502")

        with pytest.raises(mc.MCPClientError, match="Internal proxy error 502"):
            mc.mcp_call("search_gmail_messages", {"query": "q"})


# ---------------------------------------------------------------------------
# sheets_getRange -> read_sheet_values
# ---------------------------------------------------------------------------

_SHEET_SAMPLE = (
    "Successfully read 3 rows from range 'Tracker!A3:F' in spreadsheet sheet-1 for user@example.com:\n"
    "Row  1: ['7/23/2025', 'Cisco', 'Software Engineer I', '', 'LinkedIn', 'Rejected']\n"
    "Row  2: ['', 'Stripe', 'Backend Engineer', '', '', '']\n"
    "Row  3: ['8/1/2025', \"O'Reilly\", 'Data: Analyst, Junior', '', '', '']\n"
    "\n"
    "Note: Requested range 'Tracker!A3:F' was clamped to 'Tracker!A3:F1002' (max 1000 rows per read). Request a later row window to continue."
)


class TestSheetsGetRange:
    def test_translates_to_new_tool_name_and_arguments(self, monkeypatch):
        calls = _stub_text(
            monkeypatch, "No data found in range 'Tracker!A3:F' for user@example.com."
        )

        mc.mcp_call("sheets_getRange", {"spreadsheetId": "sheet-1", "range": "Tracker!A3:F"})

        assert calls == [
            (
                "read_sheet_values",
                {
                    "spreadsheet_id": "sheet-1",
                    "range_name": "Tracker!A3:F",
                    "user_google_email": _EMAIL,
                },
            )
        ]

    def test_rows_parse_with_trailing_padding_stripped(self, monkeypatch):
        # The server pads every row with '' to the first row's width; the raw
        # Sheets API path returns ragged rows, so padding must come back off.
        _stub_text(monkeypatch, _SHEET_SAMPLE)

        result = mc.mcp_call("sheets_getRange", {"spreadsheetId": "s", "range": "Tracker!A3:F"})

        assert result == {
            "values": [
                ["7/23/2025", "Cisco", "Software Engineer I", "", "LinkedIn", "Rejected"],
                ["", "Stripe", "Backend Engineer"],
                ["8/1/2025", "O'Reilly", "Data: Analyst, Junior"],
            ]
        }

    def test_no_data_text_maps_to_empty_values(self, monkeypatch):
        _stub_text(monkeypatch, "No data found in range 'Tracker!A3:F' for user@example.com.")

        result = mc.mcp_call("sheets_getRange", {"spreadsheetId": "s", "range": "Tracker!A3:F"})

        assert result == {"values": []}

    def test_malformed_row_line_raises_with_snippet(self, monkeypatch):
        text = (
            "Successfully read 2 rows from range 'T!A1:B' in spreadsheet s for user@example.com:\n"
            "Row  1: ['a', 'b']\n"
            "Row  2: not a list at all"
        )
        _stub_text(monkeypatch, text)

        with pytest.raises(mc.MCPClientError, match="not a list at all"):
            mc.mcp_call("sheets_getRange", {"spreadsheetId": "s", "range": "T!A1:B"})

    def test_malformed_header_raises_with_snippet(self, monkeypatch):
        _stub_text(monkeypatch, "Error 403: caller lacks permission")

        with pytest.raises(mc.MCPClientError, match="caller lacks permission"):
            mc.mcp_call("sheets_getRange", {"spreadsheetId": "s", "range": "T!A1:B"})


# ---------------------------------------------------------------------------
# Dispatch and configuration errors
# ---------------------------------------------------------------------------


class TestDispatchAndConfig:
    def test_unmapped_tool_name_raises_naming_the_tool(self, monkeypatch):
        _stub_text(monkeypatch, "irrelevant")

        with pytest.raises(mc.MCPClientError, match="gmail_send"):
            mc.mcp_call("gmail_send", {})

    def test_missing_user_google_email_raises_naming_the_variable(self, monkeypatch):
        monkeypatch.delenv("USER_GOOGLE_EMAIL", raising=False)
        _stub_text(monkeypatch, "irrelevant")

        with pytest.raises(mc.MCPClientError, match="USER_GOOGLE_EMAIL"):
            mc.mcp_call("gmail_get", {"messageId": "x"})

    def test_missing_server_url_raises_naming_the_variable(self, monkeypatch):
        # Transport NOT stubbed: the real path must fail on the missing URL
        # before any connection attempt (and without starting the loop thread).
        monkeypatch.delenv("MCP_SERVER_URL", raising=False)

        with pytest.raises(mc.MCPClientError, match="MCP_SERVER_URL"):
            mc.mcp_call("gmail_get", {"messageId": "x"})

        assert mc._session is None
        assert mc._loop is None

    def test_missing_required_argument_raises(self, monkeypatch):
        _stub_text(monkeypatch, "irrelevant")

        with pytest.raises(mc.MCPClientError, match="messageId"):
            mc.mcp_call("gmail_get", {})
