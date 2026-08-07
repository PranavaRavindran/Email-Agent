from datetime import datetime

from find_application_date import (
    _looks_like_confirmation,
    _same_role,
    _select_confirmation,
    find_application_date,
    is_confirmation_email,
)

_CONFIRMATION_BODY = (
    "Thank you for applying to the {role} position. We have received your application."
)
_REJECTION_BODY = (
    "We have decided not to move forward with your application for the {role} position."
)
_OTHER_BODY = "Please verify your email address to complete your account setup."


def _candidate(date_str: str, role_text: str, kind: str, candidate_id: str = None) -> dict:
    if kind == "confirmation":
        body = _CONFIRMATION_BODY.format(role=role_text)
    elif kind == "rejection":
        body = _REJECTION_BODY.format(role=role_text)
    else:
        body = _OTHER_BODY

    return {
        "id": candidate_id or f"id-{date_str}",
        "date": datetime.strptime(date_str, "%Y-%m-%d"),
        "subject": f"{role_text} - {kind}",
        "body": body,
    }


def _fake_classify_intent(subject: str, body: str) -> dict:
    """Stand-in for the real Gemini call: reads the 'kind' suffix that
    _candidate() encodes into the subject, so tests can assert on the
    surrounding scan/fetch logic without hitting the network. The real
    semantic judgment quality belongs to evals, not this unit test."""
    if subject.endswith("- confirmation"):
        return {"application_related": True, "communicates_outcome": False, "feedback_only": False}
    if subject.endswith("- rejection"):
        return {"application_related": True, "communicates_outcome": True, "feedback_only": False}
    return {"application_related": False, "communicates_outcome": False, "feedback_only": False}


def _patch_classify_intent(monkeypatch):
    import find_application_date as fad

    monkeypatch.setattr(fad, "_classify_intent", _fake_classify_intent)


# ---------------------------------------------------------------------------
# _select_confirmation - the SAIC shape
# ---------------------------------------------------------------------------


class TestSelectConfirmationSaicShape:
    def _candidates(self):
        return [
            _candidate(
                "2026-04-14", "Software Engineer Associate", "confirmation", "id-old-confirm"
            ),
            _candidate("2026-05-04", "Software Engineer Associate", "rejection", "id-old-reject"),
            _candidate("2026-05-13", "Software Engineer II", "confirmation", "id-new-confirm"),
        ]

    def test_finds_most_recent_confirmation_for_correct_role(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        result = _select_confirmation(self._candidates(), "Software Engineer II", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-13", "email_id": "id-new-confirm"}

    def test_finds_older_confirmation_when_searching_before_its_own_rejection(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        result = _select_confirmation(
            self._candidates(), "Software Engineer Associate", "2026-05-04"
        )
        assert result == {"found": True, "date": "2026-04-14", "email_id": "id-old-confirm"}

    def test_stops_scanning_at_first_match_not_examining_all_candidates(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        # A third, older candidate that would also match role B if reached -
        # if the scan didn't stop at the newest match, this earlier
        # candidate would still satisfy the confirmation check and the
        # test would still pass by accident, so instead we assert on the
        # id/date returned pointing at the newest candidate only.
        candidates = self._candidates() + [
            _candidate("2026-01-01", "Software Engineer II", "confirmation", "id-decoy-old"),
        ]
        result = _select_confirmation(candidates, "Software Engineer II", "2026-05-31")
        assert result["email_id"] == "id-new-confirm"
        assert result["date"] == "2026-05-13"


# ---------------------------------------------------------------------------
# _select_confirmation - intervening outcome stop
# ---------------------------------------------------------------------------


class TestSelectConfirmationInterveningOutcome:
    def test_matching_outcome_stops_search_before_reaching_older_confirmation(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        candidates = [
            _candidate("2026-04-14", "Backend Engineer", "confirmation", "id-true-confirm"),
            _candidate("2026-05-04", "Backend Engineer", "rejection", "id-matching-reject"),
        ]
        result = _select_confirmation(candidates, "Backend Engineer", "2026-05-31")
        assert result == {"found": False, "date": "", "email_id": ""}

    def test_outcome_for_a_different_role_is_skipped_not_stopped(self, monkeypatch):
        # The rejection names a different, identifiable role ("Data
        # Analyst") via boilerplate restating the application before
        # delivering the outcome - common in real status-update emails, and
        # exactly the pattern _classify_intent's own docstring calls out.
        # Because it IS identifiable (word-overlaps nothing with the sought
        # "Backend Engineer"), Fix 3's ambiguous-default-to-stop rule never
        # applies here - it only applies when NEITHER side can be
        # identified at all (see the Twitch-shaped test below).
        import find_application_date as fad

        def classify(subject, body):
            if "rejection" in subject.lower():
                return {
                    "application_related": True,
                    "communicates_outcome": True,
                    "feedback_only": False,
                }
            if "confirmation" in subject.lower():
                return {
                    "application_related": True,
                    "communicates_outcome": False,
                    "feedback_only": False,
                }
            return {
                "application_related": False,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        monkeypatch.setattr(fad, "_classify_intent", classify)
        candidates = [
            _candidate("2026-04-14", "Backend Engineer", "confirmation", "id-true-confirm"),
            {
                "id": "id-unrelated-reject",
                "date": datetime(2026, 5, 4),
                "subject": "Update on your Data Analyst application - rejection",
                "body": (
                    "We have received your application for Data Analyst. "
                    "After careful review, we have decided not to move "
                    "forward with your candidacy at this time."
                ),
            },
        ]
        result = _select_confirmation(candidates, "Backend Engineer", "2026-05-31")
        assert result == {"found": True, "date": "2026-04-14", "email_id": "id-true-confirm"}

    def test_outcome_sharing_one_incidental_word_does_not_stop_scan(self, monkeypatch):
        # Regression: SpaceX / Tracking (Starshield). A rejection for a
        # DIFFERENT role, "Software Engineer, Satellite Operations
        # (Starshield)", shares only the word "Starshield" with the sought
        # role. _same_role's bare word-overlap fallback treated one shared
        # word out of two as a role match and stopped the scan, discarding
        # a real, reachable older confirmation. _outcome_role_matches must
        # not use that lenient fallback for the stop decision.
        _patch_classify_intent(monkeypatch)
        candidates = [
            _candidate("2026-04-14", "Tracking (Starshield)", "confirmation", "id-true-confirm"),
            {
                "id": "id-other-role-reject",
                "date": datetime(2026, 5, 4),
                "subject": "Software Engineer, Satellite Operations (Starshield) - rejection",
                "body": (
                    "In response to your application for Software Engineer, "
                    "Satellite Operations (Starshield), we have decided not "
                    "to move forward with your candidacy at this time."
                ),
            },
        ]
        result = _select_confirmation(candidates, "Tracking (Starshield)", "2026-05-31")
        assert result == {"found": True, "date": "2026-04-14", "email_id": "id-true-confirm"}


# ---------------------------------------------------------------------------
# _select_confirmation - cap on candidates examined
# ---------------------------------------------------------------------------


class TestSelectConfirmationCap:
    def test_stops_examining_after_ten_candidates(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        # 11 non-matching candidates followed by a matching one at the very
        # back; the cap should mean it's never reached.
        candidates = [
            _candidate(f"2026-01-{day:02d}", "Irrelevant Role", "other", f"id-noise-{day}")
            for day in range(1, 12)
        ]
        candidates.append(
            _candidate("2025-12-01", "Backend Engineer", "confirmation", "id-too-old")
        )
        result = _select_confirmation(candidates, "Backend Engineer", "2026-05-31")
        assert result == {"found": False, "date": "", "email_id": ""}

    def test_before_date_excludes_same_day_candidate(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        candidates = [_candidate("2026-05-04", "Backend Engineer", "confirmation", "id-same-day")]
        result = _select_confirmation(candidates, "Backend Engineer", "2026-05-04")
        assert result == {"found": False, "date": "", "email_id": ""}


# ---------------------------------------------------------------------------
# _select_confirmation - lazy body fetch
# ---------------------------------------------------------------------------


class TestSelectConfirmationLazyFetch:
    def test_body_is_fetched_only_for_the_matching_candidate(self, monkeypatch):
        import find_application_date as fad

        fetched_ids = []

        def fake_get_email_detail(email_id):
            fetched_ids.append(email_id)
            body = {
                "id-new-confirm": _CONFIRMATION_BODY.format(role="Software Engineer II"),
                "id-old-reject": _REJECTION_BODY.format(role="Software Engineer Associate"),
                "id-old-confirm": _CONFIRMATION_BODY.format(role="Software Engineer Associate"),
            }[email_id]
            return {"email": {"body": body}}

        monkeypatch.setattr(fad, "get_email_detail", fake_get_email_detail)
        _patch_classify_intent(monkeypatch)

        candidates = [
            {
                "id": "id-old-confirm",
                "date": datetime(2026, 4, 14),
                "subject": "Software Engineer Associate - confirmation",
            },
            {
                "id": "id-old-reject",
                "date": datetime(2026, 5, 4),
                "subject": "Software Engineer Associate - rejection",
            },
            {
                "id": "id-new-confirm",
                "date": datetime(2026, 5, 13),
                "subject": "Software Engineer II - confirmation",
            },
        ]

        result = fad._select_confirmation(candidates, "Software Engineer II", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-13", "email_id": "id-new-confirm"}
        assert fetched_ids == ["id-new-confirm"]

    def test_subject_ruled_out_email_never_has_its_body_fetched(self, monkeypatch):
        import find_application_date as fad

        fetched_ids = []
        monkeypatch.setattr(
            fad,
            "get_email_detail",
            lambda email_id: (
                fetched_ids.append(email_id) or {"email": {"body": "should never be read"}}
            ),
        )

        def classify(subject, body):
            if subject == "Confirm your identity":
                return {
                    "application_related": False,
                    "communicates_outcome": False,
                    "feedback_only": False,
                }
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        monkeypatch.setattr(fad, "_classify_intent", classify)

        candidates = [
            {
                "id": "id-irrelevant",
                "date": datetime(2026, 5, 1),
                "subject": "Confirm your identity",
            },
        ]
        result = fad._select_confirmation(candidates, "Backend Engineer", "2026-05-31")
        assert result == {"found": False, "date": "", "email_id": ""}
        assert fetched_ids == []


# ---------------------------------------------------------------------------
# _looks_like_confirmation / _same_role
# ---------------------------------------------------------------------------


class TestClassificationHelpers:
    def test_same_role_prefers_requisition_number_when_both_present(self):
        body = "Thanks for applying. Requisition Number: 2612488."
        assert _same_role(body, "Software Engineer Associate (Req 2612488)") is True
        assert _same_role(body, "Software Engineer Associate (Req 2611377)") is False

    def test_same_role_falls_back_to_word_overlap_without_req_numbers(self):
        body = "Thanks for applying to the Backend Engineer role."
        assert _same_role(body, "Backend Engineer") is True
        assert _same_role(body, "Marketing Manager") is False


class TestLooksLikeConfirmation:
    """Covers the four real-world cases from the live run. The Gemini call
    is mocked with the correct semantic judgment for each case, since real
    model-quality verification belongs to evals, not this unit test — these
    tests exercise the surrounding logic: application-related AND no
    outcome AND same role => confirmation."""

    def test_real_confirmation_email_is_recognised(self, monkeypatch):
        import find_application_date as fad

        subject = "Your recent job application for Entry-Level Java Developer - 2612488"
        body = (
            "We are writing to confirm that we have received your recent "
            "application for Entry-Level Java Developer - 2612488. You can "
            "view your profile and application status online at any time."
        )
        monkeypatch.setattr(
            fad,
            "_classify_intent",
            lambda s, b: {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            },
        )
        assert (
            fad._looks_like_confirmation(subject, body, "Entry-Level Java Developer - 2612488")
            is True
        )

    def test_status_update_with_rejection_body_is_not_a_confirmation(self, monkeypatch):
        import find_application_date as fad

        subject = "SAIC – Job Application Status Update"
        body = (
            "Thank you for your interest in SAIC. After careful "
            "consideration, we have decided not to move forward with your "
            "application at this time."
        )
        monkeypatch.setattr(
            fad,
            "_classify_intent",
            lambda s, b: {
                "application_related": True,
                "communicates_outcome": True,
                "feedback_only": False,
            },
        )
        assert fad._looks_like_confirmation(subject, body, "Software Engineer Associate") is False

    def test_confirm_your_identity_is_not_application_related(self, monkeypatch):
        import find_application_date as fad

        calls = []

        def classify(subject, body):
            calls.append((subject, body))
            return {
                "application_related": False,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        monkeypatch.setattr(fad, "_classify_intent", classify)
        assert (
            fad._looks_like_confirmation("Confirm your identity", "", "Backend Engineer") is False
        )
        assert calls == [("Confirm your identity", "")]

    def test_interview_invitation_is_not_a_confirmation(self, monkeypatch):
        import find_application_date as fad

        subject = "Interview invitation for Backend Engineer"
        body = "We would like to invite you to interview for the Backend Engineer role. Please pick a time that works for you."
        monkeypatch.setattr(
            fad,
            "_classify_intent",
            lambda s, b: {
                "application_related": True,
                "communicates_outcome": True,
                "feedback_only": False,
            },
        )
        assert fad._looks_like_confirmation(subject, body, "Backend Engineer") is False


# ---------------------------------------------------------------------------
# find_application_date - end to end with a mocked Gmail service
# ---------------------------------------------------------------------------


def _metadata_response(subject: str, date: str) -> dict:
    return {
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ]
        }
    }


class _FakeExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeMessages:
    def __init__(self, emails, queries=None):
        self._emails = emails
        # When provided, every `q` a caller passes to list() is appended
        # here, so tests can assert which Gmail query was actually issued
        # (identifier-first vs. company-token) without caring about result
        # filtering, which this fake doesn't implement.
        self._queries = queries

    def list(self, **kwargs):
        if self._queries is not None:
            self._queries.append(kwargs.get("q", ""))
        return _FakeExecutable({"messages": [{"id": i} for i in self._emails]})

    def get(self, userId, id, format=None, metadataHeaders=None):
        email = self._emails[id]
        return _FakeExecutable(_metadata_response(email["subject"], email["date"]))


class _FakeUsers:
    def __init__(self, emails, queries=None):
        self._emails = emails
        self._queries = queries

    def messages(self):
        return _FakeMessages(self._emails, self._queries)


class _FakeService:
    def __init__(self, emails, queries=None):
        self._emails = emails
        self._queries = queries

    def users(self):
        return _FakeUsers(self._emails, self._queries)


def test_find_application_date_end_to_end_returns_newest_matching_confirmation(monkeypatch):
    import find_application_date as fad

    saic_emails = {
        "id-old-confirm": {
            "id": "id-old-confirm",
            "from": "SAIC <no-reply@saic.com>",
            "to": "me@example.com",
            "subject": "Software Engineer Associate application received",
            "date": "Tue, 14 Apr 2026 09:00:00 -0400",
            "body": _CONFIRMATION_BODY.format(role="Software Engineer Associate"),
        },
        "id-old-reject": {
            "id": "id-old-reject",
            "from": "SAIC <no-reply@saic.com>",
            "to": "me@example.com",
            "subject": "Update on your Software Engineer Associate application",
            "date": "Mon, 04 May 2026 09:00:00 -0400",
            "body": _REJECTION_BODY.format(role="Software Engineer Associate"),
        },
        "id-new-confirm": {
            "id": "id-new-confirm",
            "from": "SAIC <no-reply@saic.com>",
            "to": "me@example.com",
            "subject": "Software Engineer II application received",
            "date": "Wed, 13 May 2026 09:00:00 -0400",
            "body": _CONFIRMATION_BODY.format(role="Software Engineer II"),
        },
    }

    def classify(subject, body):
        lower = subject.lower()
        if "received" in lower:
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }
        if "update on your" in lower:
            return {
                "application_related": True,
                "communicates_outcome": True,
                "feedback_only": False,
            }
        return {"application_related": False, "communicates_outcome": False, "feedback_only": False}

    monkeypatch.setattr(fad, "get_gmail_service", lambda: _FakeService(saic_emails))
    monkeypatch.setattr(fad, "get_last_search_range", lambda: ("", ""))
    monkeypatch.setattr(fad, "_classify_intent", classify)
    monkeypatch.setattr(
        fad,
        "get_email_detail",
        lambda email_id: {"email": saic_emails[email_id]},
    )

    result = fad.find_application_date("SAIC", "Software Engineer II", "2026-05-31")
    assert result == {"found": True, "date": "2026-05-13", "email_id": "id-new-confirm"}


# ---------------------------------------------------------------------------
# _role_matches - role appears verbatim in the candidate subject (Fix 2)
# ---------------------------------------------------------------------------


class TestRoleMatches:
    def test_role_verbatim_in_candidate_subject_matches(self):
        import find_application_date as fad

        subject = "Your application as Junior Engineer - AI Process Optimization"
        body = "We have received your application. Our team will follow up soon."
        assert fad._role_matches(subject, body, "Junior Engineer - AI Process Optimization") is True

    def test_different_role_named_in_subject_does_not_match(self):
        import find_application_date as fad

        subject = "Your application as Data Analyst"
        body = "We have received your application. Our team will follow up soon."
        assert fad._role_matches(subject, body, "Software Engineer") is False


# ---------------------------------------------------------------------------
# _select_confirmation - weak matches when no role is named anywhere (Fix 3)
# ---------------------------------------------------------------------------


def _confirmation_classify(subject: str, body: str) -> dict:
    """Stand-in classifier: every candidate here looks like a genuine,
    outcome-less application confirmation. Exercises the role-extraction
    and weak-match logic in isolation from intent classification."""
    return {"application_related": True, "communicates_outcome": False, "feedback_only": False}


def _confirmation_classify_with_rejection(subject: str, body: str) -> dict:
    """Stand-in classifier: reads a "- confirmation"/"- rejection" suffix
    off the subject, like _fake_classify_intent, but tolerant of subjects
    that don't otherwise match _candidate()'s "{role} - {kind}" shape."""
    lower = subject.lower()
    if lower.endswith("- rejection"):
        return {"application_related": True, "communicates_outcome": True, "feedback_only": False}
    if lower.endswith("- confirmation"):
        return {"application_related": True, "communicates_outcome": False, "feedback_only": False}
    return {"application_related": False, "communicates_outcome": False, "feedback_only": False}


class TestWeakMatch:
    def test_confirmation_naming_no_role_is_a_weak_match_not_a_rejection(self, monkeypatch):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify)

        candidates = [
            {
                "id": "id-twitch",
                "date": datetime(2026, 5, 1),
                "subject": "Thank you for applying to Twitch",
                "body": "We've received your application. Our team will follow up if there's a match.",
            }
        ]
        result = fad._select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-01", "email_id": "id-twitch"}

    def test_strong_match_preferred_over_an_earlier_weak_match(self, monkeypatch):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify)

        candidates = [
            {
                "id": "id-weak-newer",
                "date": datetime(2026, 5, 10),
                "subject": "Thank you for applying to Twitch",
                "body": "We've received your application. Our team will follow up if there's a match.",
            },
            {
                "id": "id-strong-older",
                "date": datetime(2026, 4, 1),
                "subject": "Your application as Software Engineer I",
                "body": "We have received your application for Software Engineer I.",
            },
        ]
        result = fad._select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        assert result == {"found": True, "date": "2026-04-01", "email_id": "id-strong-older"}

    def test_weak_match_survives_a_same_role_outcome_that_stops_the_scan(self, monkeypatch):
        # Regression: IBM / Software Developer 2026 ELH recorded a weak
        # match, then a later same-role outcome stopped the scan and the
        # weak match was discarded, returning found=False. A weak match
        # must be returned whenever the scan terminates, including
        # termination via the stop-early rule.
        _patch_classify_intent(monkeypatch)
        candidates = [
            {
                "id": "id-weak",
                "date": datetime(2026, 5, 10),
                "subject": "Thank you for applying to Twitch - confirmation",
                "body": "We've received your application. Our team will follow up if there's a match.",
            },
            _candidate("2026-04-01", "Software Engineer I", "rejection", "id-matching-reject"),
        ]
        result = _select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-10", "email_id": "id-weak"}

    def test_weak_match_rejected_when_body_requisition_differs_from_known(self, monkeypatch):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify)

        candidates = [
            {
                "id": "id-other-req",
                "date": datetime(2026, 5, 1),
                "subject": "You have successfully submitted your IBM job application - 115821",
                "body": "Requisition Number: 115821. Thank you for applying.",
            },
        ]
        result = fad._select_confirmation(
            candidates,
            "Software Developer 2026 ELH",
            "2026-05-31",
            source_text="Update on your application. Requisition Number: 203991.",
        )
        assert result == {"found": False, "date": "", "email_id": ""}

    def test_weak_match_accepted_when_no_known_requisition_number(self, monkeypatch):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify)

        candidates = [
            {
                "id": "id-some-req",
                "date": datetime(2026, 5, 1),
                "subject": "You have successfully submitted your IBM job application - 115821",
                "body": "Requisition Number: 115821. Thank you for applying.",
            },
        ]
        result = fad._select_confirmation(candidates, "Software Developer 2026 ELH", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-01", "email_id": "id-some-req"}


# ---------------------------------------------------------------------------
# is_confirmation_email - feedback-only emails vs. confirmations whose
# footer merely invites feedback (Fix 1)
# ---------------------------------------------------------------------------


class TestFeedbackOnlyIsNotConfirmation:
    """A literal-phrase feedback/survey regex applied to the full body was
    matching nearly every recruiting confirmation footer ("share your
    feedback...") and overriding the semantic classifier, skipping genuine
    confirmations like AXS, Hypha, Navan, SpaceX, GE Vernova, DocuWare, and
    ADT. The distinction now lives in _classify_intent's own judgment via
    the feedback_only key, folded into is_confirmation_email."""

    def test_feedback_only_survey_is_not_a_confirmation(self, monkeypatch):
        import find_application_date as fad

        def classify(subject, body):
            # The email's entire purpose is the survey ask - nothing about
            # the application is acknowledged.
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": True,
            }

        monkeypatch.setattr(fad, "_classify_intent", classify)

        subject = "Tell us about your recent experience applying at ADT"
        body = (
            "We'd love your feedback on your recent application experience. "
            "Please take our short survey."
        )

        assert fad.is_confirmation_email(subject, body) is False

    def test_confirmation_whose_footer_merely_invites_feedback_is_still_a_confirmation(
        self, monkeypatch
    ):
        import find_application_date as fad

        def classify(subject, body):
            # Real case from the live run: a genuine confirmation whose
            # footer happens to invite feedback must not be excluded.
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        monkeypatch.setattr(fad, "_classify_intent", classify)

        subject = "Thank you for applying to AXS for Software Engineer!"
        body = "Thanks for applying! We received your application. Share your feedback: link"

        assert fad.is_confirmation_email(subject, body) is True


# ---------------------------------------------------------------------------
# _role_matches - a requisition mismatch is a hard NO (Fix 1)
# ---------------------------------------------------------------------------


class TestIdentifierHardNo:
    """Live evidence: IBM / Software Developer and IBM / Software Developer
    2026 ELH both matched req 115828's confirmation (Associate Application
    Developer AWS - a third, unrelated IBM application) purely on shared
    words like "IBM job application". A requisition/job identifier that
    disagrees between the sought side and the candidate must override any
    amount of word overlap."""

    def test_ibm_requisition_mismatch_overrides_shared_words(self):
        import find_application_date as fad

        role = "IBM job application - Software Developer 2026 ELH (115821)"
        subject = "You have successfully submitted your IBM job application - 115828"
        body = "Requisition Number: 115828. Thank you for your IBM job application."

        assert fad._role_matches(subject, body, role) is False

    def test_boeing_requisition_mismatch_overrides_similar_titles(self):
        import find_application_date as fad

        # Real case: JR2026508468 (Entry Level Software Engineer -
        # Simulation) incorrectly matched the confirmation for
        # JR2026512717 (a different, similarly-titled Boeing posting).
        role = "Entry Level Software Engineer–Simulation (JR2026508468)"
        subject = "Thank you for your application for JR2026512717 Entry-Level Software Engineer–Developer with Boeing"
        body = "We have received your application for requisition JR2026512717."

        assert fad._role_matches(subject, body, role) is False

    def test_shared_identifier_matches_despite_different_titles(self):
        import find_application_date as fad

        role = "Entry-Level Backend Java Developer (Req 2612488)"
        subject = "Your recent job application for Entry-Level Java Developer - 2612488"
        body = (
            "We are writing to confirm that we have received your recent "
            "application for Entry-Level Java Developer - 2612488."
        )

        assert fad._role_matches(subject, body, role) is True

    def test_select_confirmation_skips_candidate_with_conflicting_identifier(self, monkeypatch):
        # End-to-end through _select_confirmation: a strong-looking
        # candidate (role-word overlap AND application-related) must still
        # be rejected outright when its identifier conflicts with the
        # sought one, rather than falling through to a weak/word match.
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify)

        candidates = [
            {
                "id": "id-115828",
                "date": datetime(2026, 6, 6),
                "subject": "You have successfully submitted your IBM job application - 115828",
                "body": "Requisition Number: 115828. Associate Application Developer AWS 2026 - FutureNow - Chicago.",
            },
        ]
        result = fad._select_confirmation(
            candidates,
            "Software Developer 2026 ELH",
            "2026-06-30",
            source_text="IBM job application confirmation. Requisition Number: 115821.",
        )
        assert result == {"found": False, "date": "", "email_id": ""}


# ---------------------------------------------------------------------------
# _role_word_match - tightened word overlap (Fix 2)
# ---------------------------------------------------------------------------


class TestTightenedWordOverlap:
    """Live evidence: "Software Developer" matched "Associate Application
    Developer AWS 2026 - FutureNow - Chicago" because half the sought
    role's words ("developer") appeared anywhere in the candidate. Raising
    the bar to 70% of the sought word count, plus requiring at least one
    shared word of length >= 5, rejects this while still accepting close
    titles like "Entry-Level Java Developer" vs "Entry-Level Backend Java
    Developer" (covered separately via the identifier rule above)."""

    def test_half_word_overlap_no_longer_matches(self):
        import find_application_date as fad

        assert (
            fad._role_word_match(
                "Associate Application Developer AWS 2026 - FutureNow - Chicago",
                "Software Developer",
            )
            is False
        )

    def test_role_matches_rejects_the_same_case_end_to_end(self):
        import find_application_date as fad

        subject = "Associate Application Developer AWS 2026 - FutureNow - Chicago - confirmation"
        body = "Thank you for applying to the Associate Application Developer AWS 2026 - FutureNow - Chicago position."

        assert fad._role_matches(subject, body, "Software Developer") is False


# ---------------------------------------------------------------------------
# _select_confirmation - stop-early restored for unidentifiable outcomes
# (Fix 3)
# ---------------------------------------------------------------------------


class TestStopEarlyOnUnidentifiableOutcome:
    """Live evidence: Twitch. "Thank you for applying to Twitch"
    (2026-05-26, a role-less weak match) was returned even though
    "Important information about your application to Twitch" (2026-05-06,
    a role-less rejection) sat right behind it. Neither names a role, so
    neither side can be textually identified - two unidentifiable emails at
    the same company, adjacent in time, are more likely the same
    application than different ones, so the scan must stop rather than dig
    further back for a possibly-wrong older confirmation."""

    def test_roleless_outcome_at_same_company_stops_the_scan(self, monkeypatch):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify_with_rejection)

        candidates = [
            {
                "id": "id-weak",
                "date": datetime(2026, 5, 26),
                "subject": "Thank you for applying to Twitch - confirmation",
                "body": "We've received your application. Our team will follow up if there's a match.",
            },
            {
                "id": "id-roleless-reject",
                "date": datetime(2026, 5, 6),
                "subject": "Important information about your application to Twitch - rejection",
                "body": "Thank you for your interest. We have decided not to move forward at this time.",
            },
            # Would only be reached if the scan wrongly continued past the
            # rejection above - proves the scan actually stopped there.
            {
                "id": "id-decoy-older",
                "date": datetime(2026, 1, 1),
                "subject": "Thank you for applying to Twitch - confirmation",
                "body": "We've received your application. Our team will follow up if there's a match.",
            },
        ]
        result = fad._select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        assert result == {"found": True, "date": "2026-05-26", "email_id": "id-weak"}

    def test_roleless_outcome_with_no_preceding_weak_match_stops_with_not_found(self, monkeypatch):
        # Same shape, but nothing was recorded as a weak match before the
        # roleless outcome is reached: the scan must still stop there
        # rather than continue to the older, would-otherwise-match
        # confirmation behind it.
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify_with_rejection)

        candidates = [
            {
                "id": "id-roleless-reject",
                "date": datetime(2026, 5, 6),
                "subject": "Important information about your application to Twitch - rejection",
                "body": "Thank you for your interest. We have decided not to move forward at this time.",
            },
            _candidate("2026-01-01", "Software Engineer I", "confirmation", "id-decoy-older"),
        ]
        result = fad._select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        assert result == {"found": False, "date": "", "email_id": ""}

    def test_prints_no_identifying_information_message(self, monkeypatch, capsys):
        import find_application_date as fad

        monkeypatch.setattr(fad, "_classify_intent", _confirmation_classify_with_rejection)

        candidates = [
            {
                "id": "id-roleless-reject",
                "date": datetime(2026, 5, 6),
                "subject": "Important information about your application to Twitch - rejection",
                "body": "Thank you for your interest. We have decided not to move forward at this time.",
            },
        ]
        fad._select_confirmation(candidates, "Software Engineer I", "2026-05-31")
        captured = capsys.readouterr()
        assert (
            "no identifying information, assuming same application, stopping search" in captured.out
        )

    def test_outcome_with_a_shared_identifier_is_not_treated_as_no_identifying_info(self):
        # An outcome that DOES carry a matching requisition number must go
        # through the normal identifier-match stop path, not the "no
        # identifying information" one - _no_identifying_info should say
        # False as soon as either side has an identifier.
        import find_application_date as fad

        assert (
            fad._no_identifying_info(
                subject="Update on requisition 108479",
                body="Requisition Number: 108479.",
                role="Software Developer (108479)",
            )
            is False
        )

    def test_no_identifying_info_true_when_neither_side_names_anything(self):
        import find_application_date as fad

        assert (
            fad._no_identifying_info(
                subject="Important information about your application to Twitch",
                body="Thank you for your interest. We have decided not to move forward at this time.",
                role="Software Engineer I",
            )
            is True
        )


# ---------------------------------------------------------------------------
# _sought_identifier / find_application_date - query by requisition first
# ---------------------------------------------------------------------------


class TestQueryByIdentifierFirst:
    """Live evidence: IBM / Software Developer - Austin, TX (req 108479) had
    a real confirmation dated 2026-04-08, five days before a 90-day lookback
    window starting 2026-04-13 - and even a longer window would have failed,
    since ~25 IBM emails sit in that range against a candidate cap of 10.
    Querying by the requisition number directly, when one is known, finds
    the exact email instead of scanning chronologically through every email
    from the company."""

    def test_sought_identifier_extracted_from_role(self):
        import find_application_date as fad

        assert fad._sought_identifier("Software Developer - Austin, TX (108479)", "") == "108479"

    def test_sought_identifier_falls_back_to_source_text(self):
        import find_application_date as fad

        assert (
            fad._sought_identifier("Software Developer - Austin, TX", "Requisition Number: 108479.")
            == "108479"
        )

    def test_sought_identifier_empty_when_neither_side_has_one(self):
        import find_application_date as fad

        assert fad._sought_identifier("Software Developer - Austin, TX", "") == ""

    def test_find_application_date_queries_by_requisition_when_identifier_known(self, monkeypatch):
        import find_application_date as fad

        ibm_emails = {
            "id-108479-confirm": {
                "id": "id-108479-confirm",
                "subject": (
                    "You have successfully submitted your IBM job "
                    "application - 108479 - Software Developer - Austin, TX"
                ),
                "date": "Wed, 08 Apr 2026 09:00:00 -0400",
                "body": "Thank you for applying. Requisition Number: 108479.",
            },
        }

        def classify(subject, body):
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        queries = []
        monkeypatch.setattr(fad, "get_gmail_service", lambda: _FakeService(ibm_emails, queries))
        monkeypatch.setattr(fad, "get_last_search_range", lambda: ("", ""))
        monkeypatch.setattr(fad, "_classify_intent", classify)
        monkeypatch.setattr(
            fad, "get_email_detail", lambda email_id: {"email": ibm_emails[email_id]}
        )

        result = fad.find_application_date(
            "IBM", "Software Developer - Austin, TX (108479)", "2026-07-12"
        )
        assert result == {"found": True, "date": "2026-04-08", "email_id": "id-108479-confirm"}
        assert len(queries) == 1
        assert "108479" in queries[0]
        assert "ibm" not in queries[0].lower()

    def test_find_application_date_falls_back_to_company_query_without_identifier(
        self, monkeypatch
    ):
        import find_application_date as fad

        emails = {
            "id-confirm": {
                "id": "id-confirm",
                "subject": "Thank you for applying to Acme",
                "date": "Wed, 08 Apr 2026 09:00:00 -0400",
                "body": "We've received your application.",
            },
        }

        def classify(subject, body):
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        queries = []
        monkeypatch.setattr(fad, "get_gmail_service", lambda: _FakeService(emails, queries))
        monkeypatch.setattr(fad, "get_last_search_range", lambda: ("", ""))
        monkeypatch.setattr(fad, "_classify_intent", classify)
        monkeypatch.setattr(fad, "get_email_detail", lambda email_id: {"email": emails[email_id]})

        result = fad.find_application_date("Acme", "Software Engineer", "2026-07-12")
        assert result == {"found": True, "date": "2026-04-08", "email_id": "id-confirm"}
        assert len(queries) == 1
        assert "acme" in queries[0].lower()

    def test_find_application_date_falls_back_to_company_when_identifier_query_finds_nothing(
        self, monkeypatch
    ):
        # The identifier query runs first but yields no confirmation (the
        # only candidate is unrelated), so a second, company-token query
        # must still run and find the real confirmation.
        import find_application_date as fad

        emails = {
            "id-unrelated": {
                "id": "id-unrelated",
                "subject": "Password reset requested",
                "date": "Wed, 08 Apr 2026 09:00:00 -0400",
                "body": "Click here to reset your password.",
            },
            "id-real-confirm": {
                "id": "id-real-confirm",
                "subject": "Thank you for applying to IBM",
                "date": "Wed, 08 Apr 2026 09:00:00 -0400",
                "body": "We've received your application.",
            },
        }

        def classify(subject, body):
            if subject == "Password reset requested":
                return {
                    "application_related": False,
                    "communicates_outcome": False,
                    "feedback_only": False,
                }
            return {
                "application_related": True,
                "communicates_outcome": False,
                "feedback_only": False,
            }

        class _TwoQueryFakeMessages(_FakeMessages):
            def list(self, **kwargs):
                self._queries.append(kwargs.get("q", ""))
                # First (identifier) query returns the unrelated email only;
                # second (company) query returns the real confirmation.
                ids = ["id-unrelated"] if len(self._queries) == 1 else ["id-real-confirm"]
                return _FakeExecutable({"messages": [{"id": i} for i in ids]})

        class _TwoQueryFakeUsers(_FakeUsers):
            def messages(self):
                return _TwoQueryFakeMessages(self._emails, self._queries)

        class _TwoQueryFakeService(_FakeService):
            def users(self):
                return _TwoQueryFakeUsers(self._emails, self._queries)

        queries = []
        monkeypatch.setattr(fad, "get_gmail_service", lambda: _TwoQueryFakeService(emails, queries))
        monkeypatch.setattr(fad, "get_last_search_range", lambda: ("", ""))
        monkeypatch.setattr(fad, "_classify_intent", classify)
        monkeypatch.setattr(fad, "get_email_detail", lambda email_id: {"email": emails[email_id]})

        result = fad.find_application_date("IBM", "Software Developer (108479)", "2026-07-12")
        assert result == {"found": True, "date": "2026-04-08", "email_id": "id-real-confirm"}
        assert len(queries) == 2
        assert "108479" in queries[0]
        assert "ibm" in queries[1].lower()


# ---------------------------------------------------------------------------
# _select_confirmation - candidate cap differs by query type (Fix 3)
# ---------------------------------------------------------------------------


class TestCandidateCapByQueryType:
    def test_constants(self):
        import find_application_date as fad

        assert fad._MAX_CANDIDATES_EXAMINED == 10
        assert fad._MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER == 25

    def test_default_cap_of_ten_misses_a_candidate_at_position_twelve(self, monkeypatch):
        _patch_classify_intent(monkeypatch)
        candidates = [
            _candidate(f"2026-01-{day:02d}", "Irrelevant Role", "other", f"id-noise-{day}")
            for day in range(1, 12)
        ]
        candidates.append(_candidate("2025-12-01", "Backend Engineer", "confirmation", "id-found"))
        result = _select_confirmation(candidates, "Backend Engineer", "2026-05-31")
        assert result == {"found": False, "date": "", "email_id": ""}

    def test_raised_cap_of_25_finds_the_same_candidate(self, monkeypatch):
        import find_application_date as fad

        _patch_classify_intent(monkeypatch)
        candidates = [
            _candidate(f"2026-01-{day:02d}", "Irrelevant Role", "other", f"id-noise-{day}")
            for day in range(1, 12)
        ]
        candidates.append(_candidate("2025-12-01", "Backend Engineer", "confirmation", "id-found"))
        result = _select_confirmation(
            candidates,
            "Backend Engineer",
            "2026-05-31",
            max_candidates=fad._MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER,
        )
        assert result == {"found": True, "date": "2025-12-01", "email_id": "id-found"}


# ---------------------------------------------------------------------------
# _MIN_LOOKBACK_DAYS / _MAX_LOOKBACK_DAYS (Fix 2)
# ---------------------------------------------------------------------------


class TestLookbackWindow:
    """Live evidence: the Austin IBM application ran 2026-04-08 to
    2026-07-12, a 95-day cycle - longer than the old 90-day minimum
    lookback. A requisition mismatch is now a hard rejection (see
    TestIdentifierHardNo), so a wider window only surfaces more candidates
    without loosening what counts as a match."""

    def test_min_and_max_lookback_days(self):
        import find_application_date as fad

        assert fad._MIN_LOOKBACK_DAYS == 180
        assert fad._MAX_LOOKBACK_DAYS == 365


# ---------------------------------------------------------------------------
# _classify_intent - retry on transient Gemini errors (Fix 1)
#
# Live evidence: two "ServerError: 503 UNAVAILABLE" errors during
# _classify_intent calls silently caused a wrong match (GE Vernova) and two
# missing entries (CrossLink, SAIC) in one run. These tests exercise the
# actual retry loop inside _classify_intent, so they mock the genai client
# directly rather than monkeypatching _classify_intent away like every other
# test in this file.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.call_count = 0

    def generate_content(self, model, contents, config):
        outcome = self._side_effects[self.call_count]
        self.call_count += 1
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


class _FakeClient:
    def __init__(self, side_effects):
        self.models = _FakeModels(side_effects)


class TestClassifyIntentRetry:
    def _patch_client(self, monkeypatch, side_effects):
        import find_application_date as fad

        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        fake_client = _FakeClient(side_effects)
        monkeypatch.setattr(fad.genai, "Client", lambda api_key: fake_client)
        monkeypatch.setattr(fad.time, "sleep", lambda seconds: None)
        return fake_client

    def test_succeeds_after_two_transient_503s(self, monkeypatch, capsys):
        import find_application_date as fad

        error = Exception("503 UNAVAILABLE")
        success_json = (
            '{"application_related": true, "communicates_outcome": false, "feedback_only": false}'
        )
        fake_client = self._patch_client(monkeypatch, [error, error, success_json])

        result = fad._classify_intent("Your application", "We received it")

        assert result == {
            "application_related": True,
            "communicates_outcome": False,
            "feedback_only": False,
        }
        assert fake_client.models.call_count == 3
        assert "ERROR classifying intent after" not in capsys.readouterr().out

    def test_falls_back_and_logs_after_three_failed_attempts(self, monkeypatch, capsys):
        import find_application_date as fad

        error = Exception("503 UNAVAILABLE")
        fake_client = self._patch_client(monkeypatch, [error, error, error])

        result = fad._classify_intent("Your application", "We received it")

        assert result == {
            "application_related": False,
            "communicates_outcome": False,
            "feedback_only": False,
        }
        assert fake_client.models.call_count == 3

        out = capsys.readouterr().out
        assert (
            "[find_application_date] ERROR classifying intent after 3 retries "
            "— treating as non-application-related: 503 UNAVAILABLE" in out
        )

    def test_does_not_retry_on_first_success(self, monkeypatch):
        import find_application_date as fad

        success_json = (
            '{"application_related": true, "communicates_outcome": true, "feedback_only": false}'
        )
        fake_client = self._patch_client(monkeypatch, [success_json])

        result = fad._classify_intent("Update", "Not moving forward")

        assert result["application_related"] is True
        assert result["communicates_outcome"] is True
        assert fake_client.models.call_count == 1
