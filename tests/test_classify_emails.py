import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.classify_email import _classify_one, classify_emails

_SNAPSHOT = Path(__file__).parent / "data" / "classify_email_prompt_snapshot.txt"

# The exact fixture the pre-change snapshot was generated from; the
# prompt-invariance test breaks if either side changes.
_SNAPSHOT_EMAIL = {
    "from": "Jordan Lee <jordan.lee@acmecorp.example>",
    "subject": "Interview availability for Software Engineer role",
    "body": (
        "Hi,\n\nThanks for applying to Acme. Could you share your availability\n"
        "for a 45-minute interview next week? Please reply by Friday.\n\n"
        "Best,\nJordan"
    ),
    "date": "Mon, 10 Aug 2026 09:15:00 -0700",
}

_SAFE_DEFAULT = {"classification": "fyi", "action_items": [], "deadline": ""}


def _response(payload):
    response = MagicMock()
    response.text = payload if isinstance(payload, str) else json.dumps(payload)
    return response


def _client(behavior):
    """A fake genai client whose generate_content behavior is keyed by the
    Subject: line of the prompt. behavior maps subject -> one of:
    a result dict, a raw string response, an Exception to raise, or a
    (delay_seconds, result_dict) tuple."""
    client = MagicMock()

    def generate_content(model, contents, config):
        subject_line = next(line for line in contents.splitlines() if line.startswith("Subject: "))
        subject = subject_line[len("Subject: ") :]
        action = behavior[subject]
        if isinstance(action, tuple):
            delay, action = action
            time.sleep(delay)
        if isinstance(action, Exception):
            raise action
        return _response(action)

    client.models.generate_content.side_effect = generate_content
    return client


def _run(emails, behavior):
    with patch("tools.classify_email.get_genai_client", return_value=_client(behavior)):
        return classify_emails(emails)


def _email(subject, **extra):
    return {"from": "sender@example.com", "subject": subject, "body": "text", **extra}


class TestPromptInvariance:
    def test_prompt_is_byte_identical_to_pre_change_snapshot(self):
        captured = {}
        client = MagicMock()

        def generate_content(model, contents, config):
            captured["prompt"] = contents
            return _response(_SAFE_DEFAULT)

        client.models.generate_content.side_effect = generate_content
        with patch("tools.classify_email.get_genai_client", return_value=client):
            _classify_one(_SNAPSHOT_EMAIL)

        assert captured["prompt"] == _SNAPSHOT.read_text(encoding="utf-8")

    def test_batch_uses_the_same_prompt_as_the_helper(self):
        prompts = []
        client = MagicMock()

        def generate_content(model, contents, config):
            prompts.append(contents)
            return _response(_SAFE_DEFAULT)

        client.models.generate_content.side_effect = generate_content
        with patch("tools.classify_email.get_genai_client", return_value=client):
            classify_emails([_SNAPSHOT_EMAIL])

        assert prompts == [_SNAPSHOT.read_text(encoding="utf-8")]


class TestClassifyEmails:
    def test_order_preserved_when_completion_is_out_of_order(self):
        emails = [_email(f"email {i}") for i in range(4)]
        # Earlier inputs finish last: email 0 is slowest, email 3 fastest.
        behavior = {
            f"email {i}": ((3 - i) * 0.05, {**_SAFE_DEFAULT, "deadline": f"d{i}"}) for i in range(4)
        }
        result = _run(emails, behavior)
        assert [entry["index"] for entry in result["results"]] == [0, 1, 2, 3]
        assert [entry["deadline"] for entry in result["results"]] == [
            "d0",
            "d1",
            "d2",
            "d3",
        ]

    def test_single_email_batch(self):
        result = _run(
            [_email("only one")],
            {
                "only one": {
                    "classification": "urgent",
                    "action_items": ["reply"],
                    "deadline": "Friday",
                }
            },
        )
        assert result == {
            "results": [
                {
                    "index": 0,
                    "subject": "only one",
                    "classification": "urgent",
                    "action_items": ["reply"],
                    "deadline": "Friday",
                }
            ]
        }

    def test_one_failure_gets_safe_default_and_rest_keep_results(self):
        result = _run(
            [_email("ok 1"), _email("boom"), _email("ok 2")],
            {
                "ok 1": {"classification": "urgent", "action_items": ["a"], "deadline": "x"},
                "boom": RuntimeError("simulated API failure"),
                "ok 2": {"classification": "action_needed", "action_items": [], "deadline": ""},
            },
        )
        classifications = [entry["classification"] for entry in result["results"]]
        assert classifications == ["urgent", "fyi", "action_needed"]
        assert result["results"][1] == {"index": 1, "subject": "boom", **_SAFE_DEFAULT}

    def test_unparseable_response_gets_safe_default(self):
        result = _run(
            [_email("garbled"), _email("fine")],
            {
                "garbled": "not json at all",
                "fine": {"classification": "spam", "action_items": [], "deadline": ""},
            },
        )
        assert result["results"][0] == {"index": 0, "subject": "garbled", **_SAFE_DEFAULT}
        assert result["results"][1]["classification"] == "spam"

    def test_all_failures_return_one_safe_default_per_input(self):
        emails = [_email(f"fail {i}") for i in range(3)]
        behavior = {f"fail {i}": RuntimeError("down") for i in range(3)}
        result = _run(emails, behavior)
        assert result["results"] == [
            {"index": i, "subject": f"fail {i}", **_SAFE_DEFAULT} for i in range(3)
        ]

    def test_snippet_only_email_is_classified_from_its_snippet(self):
        prompts = []
        client = MagicMock()

        def generate_content(model, contents, config):
            prompts.append(contents)
            return _response(_SAFE_DEFAULT)

        client.models.generate_content.side_effect = generate_content
        email = {
            "from": "sender@example.com",
            "subject": "snippet only",
            "snippet": "Your application is under review",
        }
        with patch("tools.classify_email.get_genai_client", return_value=client):
            classify_emails([email])

        assert "Body: Your application is under review" in prompts[0]

    def test_index_and_subject_echoed_on_every_result(self):
        emails = [_email("first"), _email("second")]
        behavior = {"first": _SAFE_DEFAULT, "second": _SAFE_DEFAULT}
        result = _run(emails, behavior)
        assert [(entry["index"], entry["subject"]) for entry in result["results"]] == [
            (0, "first"),
            (1, "second"),
        ]

    def test_empty_batch_returns_empty_results(self):
        assert classify_emails([]) == {"results": []}
