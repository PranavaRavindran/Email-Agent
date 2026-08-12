import get_email_detail as ged
import pytest


def _fake_service(call_log, subject="Hello", body_text="Body text"):
    class _FakeExecutable:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    class _FakeMessages:
        def get(self, userId, id, format=None):
            call_log.append(id)
            return _FakeExecutable(
                {
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": subject},
                            {"name": "From", "value": "a@b.com"},
                            {"name": "To", "value": "me@example.com"},
                            {"name": "Date", "value": "Tue, 14 Apr 2026 09:00:00 -0400"},
                        ],
                        "mimeType": "text/plain",
                        "body": {"data": ged.base64.urlsafe_b64encode(body_text.encode()).decode()},
                    }
                }
            )

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    return _FakeService()


class TestBodyCache:
    def setup_method(self):
        ged.reset_fetched_ids()

    def teardown_method(self):
        ged.reset_fetched_ids()

    def test_second_fetch_of_same_id_hits_cache_not_gmail(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        first = ged.get_email_detail("id-1")
        second = ged.get_email_detail("id-1")

        assert call_log == ["id-1"]
        assert first == second

    def test_cache_hit_still_records_fetched_id(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        assert "id-1" in ged.get_fetched_ids()

        ged.reset_fetched_ids()
        assert "id-1" not in ged.get_fetched_ids()

    def test_reset_fetched_ids_clears_cache_too(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        ged.reset_fetched_ids()
        ged.get_email_detail("id-1")

        assert call_log == ["id-1", "id-1"]

    def test_different_ids_are_not_conflated(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        ged.get_email_detail("id-2")

        assert call_log == ["id-1", "id-2"]


# ---------------------------------------------------------------------------
# MCP fetch path (USE_MCP_GMAIL=1)
# ---------------------------------------------------------------------------


class TestMCPFetchPath:
    def setup_method(self):
        ged.reset_fetched_ids()

    def teardown_method(self):
        ged.reset_fetched_ids()

    def test_mcp_response_maps_to_wrapper_shape_truncates_and_records_id(self, monkeypatch):
        monkeypatch.setenv("USE_MCP_GMAIL", "1")
        # Representative gmail_get (format=full) response, per
        # GmailService.ts's `get` handler - see MCP_INTEGRATION.md gate G2.
        long_body = "x" * 2500
        fixture = {
            "id": "id-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX"],
            "snippet": "x" * 100,
            "subject": "Hello",
            "from": "a@b.com",
            "to": "me@example.com",
            "date": "Tue, 14 Apr 2026 09:00:00 -0400",
            "body": long_body,
            "attachments": [],
        }
        calls = []

        def fake_mcp_call(tool_name, arguments):
            calls.append((tool_name, arguments))
            return fixture

        monkeypatch.setattr(ged, "mcp_call", fake_mcp_call)

        result = ged.get_email_detail("id-1")

        assert calls == [("gmail_get", {"messageId": "id-1"})]
        assert result == {
            "email": {
                "id": "id-1",
                "from": "a@b.com",
                "to": "me@example.com",
                "subject": "Hello",
                "date": "Tue, 14 Apr 2026 09:00:00 -0400",
                "body": "x" * 2000 + "...[truncated]",
            }
        }
        assert "id-1" in ged.get_fetched_ids()

    def test_mcp_error_propagates_and_does_not_record_fetched_id(self, monkeypatch):
        monkeypatch.setenv("USE_MCP_GMAIL", "1")

        def fake_mcp_call(tool_name, arguments):
            raise RuntimeError("boom")

        monkeypatch.setattr(ged, "mcp_call", fake_mcp_call)

        with pytest.raises(RuntimeError, match="boom"):
            ged.get_email_detail("id-1")

        assert "id-1" not in ged.get_fetched_ids()


# ---------------------------------------------------------------------------
# HTML body normalization (the 2026-08-11 MCP drafting regression)
# ---------------------------------------------------------------------------


def _mcp_fixture(body):
    return {
        "id": "id-1",
        "threadId": "thread-1",
        "labelIds": ["INBOX"],
        "snippet": "snippet text",
        "subject": "Hello",
        "from": "a@b.com",
        "to": "me@example.com",
        "date": "Tue, 14 Apr 2026 09:00:00 -0400",
        "body": body,
        "attachments": [],
    }


class TestMCPHtmlBodyNormalization:
    def setup_method(self):
        ged.reset_fetched_ids()

    def teardown_method(self):
        ged.reset_fetched_ids()

    def _fetch_with_mcp_body(self, monkeypatch, body):
        monkeypatch.setenv("USE_MCP_GMAIL", "1")
        monkeypatch.setattr(ged, "mcp_call", lambda tool, args: _mcp_fixture(body))
        return ged.get_email_detail("id-1")["email"]["body"]

    def test_mcp_html_only_body_yields_prose_not_markup(self, monkeypatch):
        html_body = (
            '<!doctype html><html lang=en><head><style type="text/css">'
            "#outlook a { padding:0; } body { margin:0;padding:0; }"
            "</style></head><body><p>Your application was not selected.</p>"
            "</body></html>"
        )

        result = self._fetch_with_mcp_body(monkeypatch, html_body)

        assert "Your application was not selected." in result
        assert "<style" not in result
        assert "<!doctype" not in result
        assert "padding:0" not in result

    def test_mcp_plain_text_body_passes_through_byte_identical(self, monkeypatch):
        plain_body = "Hi,\n\nWe received your form (score < 5 > 3).\n\nThanks"

        result = self._fetch_with_mcp_body(monkeypatch, plain_body)

        assert result == plain_body

    def test_mcp_html_markup_over_limit_prose_survives_truncation(self, monkeypatch):
        # The exact Cisco shape: a full HTML document whose markup exceeds
        # _MAX_BODY_LENGTH before the prose even starts, while the prose
        # itself is tiny. Without extraction-before-truncation the model
        # sees only doctype/CSS preamble.
        prose = "We regret to inform you that your application was not selected."
        css_filler = "#outlook a { padding:0; } body { margin:0;padding:0; } " * 60
        html_body = (
            '<!doctype html><html lang=en><head><style type="text/css">'
            + css_filler
            + "</style></head><body><p>"
            + prose
            + "</p></body></html>"
        )
        assert len(html_body) > ged._MAX_BODY_LENGTH
        assert html_body.index(prose) > ged._MAX_BODY_LENGTH

        result = self._fetch_with_mcp_body(monkeypatch, html_body)

        assert prose in result
        assert "[truncated]" not in result
        assert "padding:0" not in result

    def test_raw_path_html_fallback_behavior_unchanged(self, monkeypatch):
        # Control arm: USE_MCP_GMAIL=0 already converts HTML via
        # _extract_body; _normalize_body must not double-process or alter it.
        monkeypatch.setenv("USE_MCP_GMAIL", "0")
        call_log = []
        html_body = "<html><body><p>Please review the attached invoice.</p></body></html>"

        class _FakeExecutable:
            def execute(self):
                return {
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Invoice"},
                            {"name": "From", "value": "a@b.com"},
                            {"name": "To", "value": "me@example.com"},
                            {"name": "Date", "value": "Tue, 14 Apr 2026 09:00:00 -0400"},
                        ],
                        "mimeType": "text/html",
                        "body": {
                            "data": ged.base64.urlsafe_b64encode(html_body.encode()).decode()
                        },
                    }
                }

        class _FakeMessages:
            def get(self, userId, id, format=None):
                call_log.append(id)
                return _FakeExecutable()

        class _FakeUsers:
            def messages(self):
                return _FakeMessages()

        class _FakeService:
            def users(self):
                return _FakeUsers()

        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _FakeService())

        result = ged.get_email_detail("id-1")["email"]["body"]

        assert result == ged._html_to_text(html_body)
        assert "Please review the attached invoice." in result
        assert "<p>" not in result

    def test_raw_path_plain_text_behavior_unchanged(self, monkeypatch):
        monkeypatch.setenv("USE_MCP_GMAIL", "0")
        call_log = []
        monkeypatch.setattr(
            ged,
            "get_thread_local_gmail_service",
            lambda: _fake_service(call_log, body_text="Plain body text"),
        )

        result = ged.get_email_detail("id-1")["email"]["body"]

        assert result == "Plain body text"
