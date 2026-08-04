import get_email_detail as ged


def _fake_service(call_log, subject="Hello", body_text="Body text"):
    class _FakeExecutable:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    class _FakeMessages:
        def get(self, userId, id, format=None):
            call_log.append(id)
            return _FakeExecutable({
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
            })

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
        monkeypatch.setattr(ged, "get_gmail_service", lambda: _fake_service(call_log))

        first = ged.get_email_detail("id-1")
        second = ged.get_email_detail("id-1")

        assert call_log == ["id-1"]
        assert first == second

    def test_cache_hit_still_records_fetched_id(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        assert "id-1" in ged.get_fetched_ids()

        ged.reset_fetched_ids()
        assert "id-1" not in ged.get_fetched_ids()

    def test_reset_fetched_ids_clears_cache_too(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        ged.reset_fetched_ids()
        ged.get_email_detail("id-1")

        assert call_log == ["id-1", "id-1"]

    def test_different_ids_are_not_conflated(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        ged.get_email_detail("id-2")

        assert call_log == ["id-1", "id-2"]
