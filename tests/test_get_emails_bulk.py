import importlib

from tools.get_emails_bulk import get_emails_bulk

# tools/__init__.py does `from .get_email_detail import get_email_detail`, which
# rebinds the `get_email_detail` attribute on the `tools` package from the
# submodule to that function. importlib.import_module sidesteps that shadow
# and always returns the real submodule, which is what get_emails_bulk.py
# itself imports from and what needs to be monkeypatched here.
ged = importlib.import_module("tools.get_email_detail")
geb = importlib.import_module("tools.get_emails_bulk")


def _fake_service(call_log, fail_ids=None, subject="Hello", body_text="Body text"):
    fail_ids = fail_ids or set()

    class _FakeExecutable:
        def __init__(self, email_id, result):
            self._id = email_id
            self._result = result

        def execute(self):
            if self._id in fail_ids:
                raise RuntimeError(f"simulated failure for {self._id}")
            return self._result

    class _FakeMessages:
        def get(self, userId, id, format=None):
            call_log.append(id)
            return _FakeExecutable(id, {
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


class TestGetEmailsBulk:
    def setup_method(self):
        ged.reset_fetched_ids()

    def teardown_method(self):
        ged.reset_fetched_ids()

    def test_only_fetches_uncached_ids(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        ged.get_email_detail("id-1")
        call_log.clear()

        results = get_emails_bulk(["id-1", "id-2", "id-3"])

        assert sorted(call_log) == ["id-2", "id-3"]
        assert set(results.keys()) == {"id-1", "id-2", "id-3"}
        for email_id, result in results.items():
            assert "error" not in result
            assert result["email"]["id"] == email_id

    def test_all_uncached_fetches_every_id_exactly_once(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log))

        results = get_emails_bulk(["id-1", "id-2", "id-3"])

        assert sorted(call_log) == ["id-1", "id-2", "id-3"]
        assert set(results.keys()) == {"id-1", "id-2", "id-3"}
        assert ged.get_fetched_ids() == {"id-1", "id-2", "id-3"}

    def test_partial_failure_returns_error_entry_and_continues(self, monkeypatch):
        call_log = []
        monkeypatch.setattr(
            ged, "get_thread_local_gmail_service", lambda: _fake_service(call_log, fail_ids={"id-2"})
        )

        results = get_emails_bulk(["id-1", "id-2", "id-3"])

        assert set(results.keys()) == {"id-1", "id-2", "id-3"}
        assert "error" in results["id-2"]
        assert "error" not in results["id-1"]
        assert "error" not in results["id-3"]
        # A failed fetch was never actually read, so it must not count as
        # fetched for stage_write's fetch-completeness guard.
        assert "id-2" not in ged.get_fetched_ids()
        assert {"id-1", "id-3"} <= ged.get_fetched_ids()

    def test_uncached_ids_fetched_via_the_shared_fetch_function(self, monkeypatch):
        # Exercises the concurrency path (ThreadPoolExecutor submitting
        # _fetch_one per uncached id) without asserting execution order,
        # which a thread pool does not guarantee.
        calls = []

        def fake_fetch_one(email_id, quiet):
            calls.append(email_id)
            return {"email": {"id": email_id, "body": "b"}}

        monkeypatch.setattr(geb, "_fetch_one", fake_fetch_one)

        results = get_emails_bulk(["id-a", "id-b", "id-c"])

        assert sorted(calls) == ["id-a", "id-b", "id-c"]
        assert set(results.keys()) == {"id-a", "id-b", "id-c"}
