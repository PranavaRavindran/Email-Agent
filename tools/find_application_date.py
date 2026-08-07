import json
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import google.genai as genai
from google.genai import types

from auth import get_gmail_service
from tools.get_email_detail import get_email_detail
from tools.search_email_ids import get_last_search_range

_MIN_LOOKBACK_DAYS = 180
_MAX_LOOKBACK_DAYS = 365
_MAX_CANDIDATES_EXAMINED = 10
_MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER = 25

_CLASSIFY_MAX_ATTEMPTS = 3
_CLASSIFY_RETRY_BACKOFF_SECONDS = (1, 2, 4)

_REQ_NUMBER_RE = re.compile(
    r"req(?:uisition)?\s*(?:id|number|no\.?|#)?\s*[:#]?\s*(\d{4,})",
    re.IGNORECASE,
)

# A requisition/job identifier: a run of 5+ digits, optionally prefixed by a
# few letters (115828, JR2026512717, R20602, 2612488, 3020510). Unlike
# _REQ_NUMBER_RE this needs no surrounding context words, so it also catches
# identifiers embedded in subjects and boilerplate that never say "req" at
# all - which is most of them.
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z]{0,4}\d{5,}\b")

_FILLER_WORDS = {
    "the",
    "for",
    "your",
    "position",
    "role",
    "job",
    "application",
    "recent",
    "new",
    "and",
}


def _company_token(company: str) -> str:
    first_word = company.strip().split(" ")[0] if company.strip() else ""
    return re.sub(r"[^a-z0-9]", "", first_word.lower())


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def _extract_req_number(text: str) -> str:
    match = _REQ_NUMBER_RE.search(text or "")
    return match.group(1) if match else ""


def _extract_identifiers(text: str) -> set:
    """Every requisition/job identifier found in `text`, uppercased for
    case-insensitive comparison. See _IDENTIFIER_RE."""
    return {m.group(0).upper() for m in _IDENTIFIER_RE.finditer(text or "")}


def _sought_identifier(role: str, source_text: str) -> str:
    """The first requisition/job identifier found in the sought side (the
    role string, else source_text), or "" if neither carries one. Used to
    query Gmail for that identifier directly instead of scanning
    chronologically through every email from the company - see
    find_application_date."""
    match = _IDENTIFIER_RE.search(role or "")
    if match:
        return match.group(0)
    match = _IDENTIFIER_RE.search(source_text or "")
    return match.group(0) if match else ""


def _identifier_verdict(sought_ids: set, candidate_ids: set):
    """The hard identifier rule shared by _role_matches, _outcome_role_matches,
    and the weak-match guard in _select_confirmation.

    Returns True (definitive match), False (definitive no-match), or None
    when either side has no identifiers at all - in which case the caller
    must fall back to textual comparison. A requisition/job id is the
    strongest signal available: two emails naming the same one are the same
    application regardless of wording, and two emails naming different ones
    are different applications regardless of shared words like a company
    name or "job application".
    """
    if sought_ids and candidate_ids:
        return bool(sought_ids & candidate_ids)
    return None


def _significant_words(text: str) -> set:
    return {
        w.lower()
        for w in re.findall(r"[A-Za-z0-9]+", text or "")
        if len(w) > 2 and w.lower() not in _FILLER_WORDS
    }


def _role_word_match(candidate_text: str, role: str) -> bool:
    """True if `candidate_text` (ideally an extracted candidate role, else
    raw body text) names substantially the same role as `role`.

    Tightened to require both: the shared significant words cover at least
    70% of the SOUGHT role's word count (not just half of whichever side is
    shorter, which let a four-word candidate role match on two incidental
    words), and at least one shared word is long enough (>=5 chars) to be a
    real distinguishing term rather than an incidental short word.
    """
    role_words = _significant_words(role)
    if not role_words:
        return True

    candidate_words = _significant_words(candidate_text)
    overlap = role_words & candidate_words
    if not overlap:
        return False

    has_distinguishing_word = any(len(w) >= 5 for w in overlap)
    return has_distinguishing_word and (len(overlap) / len(role_words)) >= 0.7


def _same_role(body: str, role: str) -> bool:
    """True if body appears to be about the same role as the target role.

    Prefers an exact requisition-number match when a req number can be
    pulled from both the target role text and the candidate body; falls
    back to word overlap on the role title otherwise, since two different
    requisitions can carry the identical title.
    """
    target_req = _extract_req_number(role)
    body_req = _extract_req_number(body)
    if target_req and body_req:
        return body_req == target_req
    return _role_word_match(body, role)


def _role_hint(subject: str, body: str) -> str:
    """A short label for what role a candidate email appears to concern, for
    role-mismatch log messages only. Not used for matching decisions."""
    body_req = _extract_req_number(body)
    if body_req:
        return f"req {body_req}"
    return subject or "unknown role"


_ROLE_PHRASE_RE = re.compile(
    r"(?:your application as|"
    r"your recent job application for|"
    r"we received your application for|"
    r"we have received your application for|"
    r"thank you for applying to .+? for|"
    r"thank you for applying for)"
    r"\s*([^.\n]+)",
    re.IGNORECASE,
)

_TRAILING_NUMBER_RE = re.compile(r"[\s\-–,(]*\d{4,}\)?\s*$")


def _extract_role(text: str) -> str:
    """Best-effort extraction of the role a candidate email names, from
    phrasing such as "your application as <role>" or "we received your
    application for <role>". Returns "" when no such phrase is found in
    `text`, which signals the email may not name a role at all - the
    trigger for treating a candidate as a weak match rather than a
    rejection (see _select_confirmation)."""
    match = _ROLE_PHRASE_RE.search(text or "")
    if not match:
        return ""
    role_text = _TRAILING_NUMBER_RE.sub("", match.group(1))
    return role_text.strip(" -–:.,")


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _contains_role(text: str, role: str) -> bool:
    """True if `role` appears as a whitespace-bounded substring of `text`,
    case-insensitively."""
    norm_text = _normalize_for_match(text)
    norm_role = _normalize_for_match(role)
    if not norm_role:
        return False
    return f" {norm_text} ".find(f" {norm_role} ") != -1


def _role_matches(subject: str, body: str, role: str, source_text: str = "") -> bool:
    """True if the candidate email appears to concern the same role as
    `role`.

    A requisition/job identifier on both sides is checked FIRST and is
    definitive: sharing one is an outright match, and carrying different
    ones is an outright non-match, full stop - no amount of shared wording
    (e.g. the same company name or the phrase "job application") can
    override a confirmed identifier mismatch. `source_text` supplies extra
    identifier context for the sought side (the email that triggered this
    lookback), since the identifier may appear there rather than in the
    role string itself.

    Only when identifiers are absent or inconclusive (present on one side
    only, or neither) does this fall back to the older textual comparison:
    an outright match when the sought role appears as a whitespace-bounded
    substring of the subject or body, then the role extracted from the
    candidate (application boilerplate and any trailing requisition number
    stripped) compared against `role`, and finally a word-overlap
    comparison via _same_role.
    """
    sought_ids = _extract_identifiers(role) | _extract_identifiers(source_text)
    candidate_ids = _extract_identifiers(subject) | _extract_identifiers(body)
    verdict = _identifier_verdict(sought_ids, candidate_ids)
    if verdict is not None:
        return verdict

    if _contains_role(subject, role) or _contains_role(body, role):
        return True

    candidate_role = _extract_role(subject) or _extract_role(body)
    if candidate_role and _role_word_match(candidate_role, role):
        return True

    return _same_role(body, role)


def _outcome_role_matches(subject: str, body: str, role: str, source_text: str = "") -> bool:
    """True if an OUTCOME email (rejection/interview/offer) concerns the
    same role as `role`, strictly enough to justify stopping the scan.

    Checked in order:
      1. Identifiers (see _identifier_verdict): sharing one is a definitive
         match/stop; carrying different ones is a definitive non-match/skip.
      2. The sought role appearing verbatim in the candidate, or an
         extracted candidate role word-overlapping the sought role.
      3. When none of the above can say anything - no identifiers, no
         extractable candidate role - falls back to raw word overlap
         between the candidate's subject+body and the sought role. ANY
         shared significant word (even one insufficient for a full
         _role_word_match) means the candidate is talking about something
         at least identifiable, so it's treated as a different, unrelated
         application and the scan continues. Zero shared words means
         neither side names anything recognisable, and two unidentifiable
         emails at the same company, adjacent in time, are more likely the
         same application than different ones - so this is treated as a
         match and the scan stops. Stopping on a false positive costs an
         approximate date; continuing on a false negative risks surfacing a
         wrong confirmation further back.
    """
    sought_ids = _extract_identifiers(role) | _extract_identifiers(source_text)
    candidate_ids = _extract_identifiers(subject) | _extract_identifiers(body)
    verdict = _identifier_verdict(sought_ids, candidate_ids)
    if verdict is not None:
        return verdict

    if _contains_role(subject, role) or _contains_role(body, role):
        return True

    candidate_role = _extract_role(subject) or _extract_role(body)
    if candidate_role:
        return _role_word_match(candidate_role, role)

    sought_words = _significant_words(role)
    candidate_words = _significant_words(f"{subject} {body}")
    if sought_words & candidate_words:
        return False

    return True


def _no_identifying_info(subject: str, body: str, role: str, source_text: str = "") -> bool:
    """True when NEITHER side of an outcome candidate carries any
    identifying information at all: no requisition/job identifier anywhere
    (sought or candidate), no role phrase extractable from the candidate,
    and not even an incidental significant word shared between the sought
    role and the candidate's subject+body.

    Mirrors, step for step, the exact conditions under which
    _outcome_role_matches falls through to its own final `return True` -
    so this is True precisely when that match is a "nothing distinguishes
    these two" default rather than a real identifier or role match. That
    keeps this in sync with _outcome_role_matches: a case like SpaceX's
    "Tracking (Starshield)" vs. a rejection for a different role that
    happens to also say "(Starshield)" shares the significant word
    "starshield", so it is NOT treated as unidentifiable here, exactly as
    _outcome_role_matches does not stop on it either. Only a pair like
    "Thank you for applying to Twitch" / "Update on your application to
    Twitch" - which names nothing recognisable on either side - is treated
    as the same application, and the caller stops the scan rather than
    digging further back for a possibly-wrong older confirmation.
    """
    sought_ids = _extract_identifiers(role) | _extract_identifiers(source_text)
    candidate_ids = _extract_identifiers(subject) | _extract_identifiers(body)
    if sought_ids or candidate_ids:
        return False

    if _contains_role(subject, role) or _contains_role(body, role):
        return False

    if _extract_role(subject) or _extract_role(body):
        return False

    sought_words = _significant_words(role)
    candidate_words = _significant_words(f"{subject} {body}")
    return not (sought_words & candidate_words)


def _classify_intent(subject: str, body: str) -> dict:
    """Judges an email's relationship to a job application by meaning rather
    than fixed phrases.

    A literal-phrase lookup table was already tried for this project's
    status classification and failed on real emails that used none of its
    terms, so this asks the model to judge the outcome actually communicated
    rather than pattern-match on wording.

    Args:
        subject: The email subject line.
        body: The email body, or "" to judge from the subject alone. Used
            to rule out obviously unrelated email before its body is
            fetched.

    Returns:
        A dict with:
          - application_related: bool
          - communicates_outcome: bool, true only when the email delivers a
            real outcome for the application: declining to advance the
            candidate, moving the candidate forward to a next step
            (interview, assessment, scheduling), or extending an offer.
            False when the email only acknowledges that an application or
            materials were received/submitted with no outcome communicated.
          - feedback_only: bool, true only when the email's ENTIRE purpose is
            soliciting feedback, a survey, or a rating about the application
            process, with nothing acknowledged and no outcome delivered.
            False for a confirmation whose footer merely invites feedback -
            judge by the email's primary purpose, not by whether the word
            "feedback" appears anywhere in it.
    """
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    prompt = f"""Judge this email's relationship to a job application the
recipient submitted.

Subject: {subject}
Body: {body}

Respond with a JSON object containing exactly these keys:
- "application_related": true if the email concerns a job application the
  recipient submitted or is following up on (an acknowledgement of
  submission, a status update, a rejection, an interview or assessment
  invitation, or an offer). false for anything else, including account
  verification, identity confirmation, password resets, or newsletters.
- "communicates_outcome": true only when application_related is true AND the
  email delivers a real outcome for that application - declining to advance
  the candidate, moving the candidate forward to a next step (interview,
  assessment, scheduling), or extending an offer. false when the email only
  acknowledges that an application or materials were received or submitted,
  with no outcome communicated, even if it also links to a profile or
  account page.
- "feedback_only": true only when the email's ENTIRE purpose is soliciting
  feedback, a survey, or a rating about the application process - nothing
  about the application is acknowledged and no outcome is delivered, the
  whole body is the feedback/survey ask. false for an application
  confirmation whose footer merely invites feedback or links to a survey in
  passing; that is still primarily a confirmation. Judge by the email's
  primary purpose, not by whether words like "feedback" or "survey" appear
  anywhere in it.

Judge by meaning, not fixed phrases - companies word these very differently,
and a literal-phrase lookup table has already failed on real emails that
used none of its terms. An email often opens with boilerplate restating the
application before delivering a different outcome; judge by the outcome
actually delivered, not the opening line. If the body is empty, judge from
the subject alone, and only set application_related true when the subject
plausibly concerns a job application - do not assume account, security, or
newsletter subjects are application-related.

Respond ONLY with the JSON object, no markdown fences or extra text."""

    last_error = None
    for attempt in range(1, _CLASSIFY_MAX_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            result = json.loads(response.text.strip())
            return {
                "application_related": bool(result.get("application_related", False)),
                "communicates_outcome": bool(result.get("communicates_outcome", False)),
                "feedback_only": bool(result.get("feedback_only", False)),
            }
        except Exception as e:
            last_error = e
            if attempt < _CLASSIFY_MAX_ATTEMPTS:
                backoff = _CLASSIFY_RETRY_BACKOFF_SECONDS[attempt - 1]
                print(
                    f"[find_application_date] WARNING classify intent attempt "
                    f"{attempt}/{_CLASSIFY_MAX_ATTEMPTS} failed "
                    f"({type(e).__name__}: {e}), retrying in {backoff}s"
                )
                time.sleep(backoff)

    print(
        f"[find_application_date] ERROR classifying intent after "
        f"{_CLASSIFY_MAX_ATTEMPTS} retries — treating as non-application-related: {last_error}"
    )
    return {"application_related": False, "communicates_outcome": False, "feedback_only": False}


def is_confirmation_email(subject: str, body: str) -> bool:
    """True when an email acknowledges an application was received or
    submitted without communicating an outcome, and is not itself purely a
    feedback/survey solicitation."""
    intent = _classify_intent(subject, body)
    return (
        intent["application_related"]
        and not intent["communicates_outcome"]
        and not intent["feedback_only"]
    )


def _looks_like_confirmation(subject: str, body: str, role: str, source_text: str = "") -> bool:
    if not is_confirmation_email(subject, body):
        return False
    return _role_matches(subject, body, role, source_text)


def _select_confirmation(
    candidates: list,
    role: str,
    before_date,
    source_text: str = "",
    max_candidates: int = _MAX_CANDIDATES_EXAMINED,
) -> dict:
    """Scans candidate emails newest-first for the application confirmation
    matching role, fetching each body lazily.

    Args:
        candidates: list of dicts with 'id', 'date' (datetime), 'subject',
            and optionally 'body'. When 'body' is absent, it is fetched via
            get_email_detail only if the candidate survives the subject-only
            check - so a match on the first candidate costs one fetch.
        role: Job title being searched for.
        before_date: datetime or YYYY-MM-DD string; only candidates strictly
            before this are considered.
        source_text: optional subject+body text of the email(s) that
            triggered this lookback (the entry's own group), used only for
            requisition/job identifier extraction on the sought side. See
            _role_matches and _outcome_role_matches.
        max_candidates: how many eligible candidates (newest-first) to
            examine before giving up. Callers pass a higher cap for an
            identifier-scoped query, where results are already narrowly
            targeted, than for a broad company-token query. Defaults to
            _MAX_CANDIDATES_EXAMINED.

    Candidates are sorted newest-first and examined in that order, returning
    on the first confirmation that matches the role: the correct application
    confirmation is always the most recent one preceding the status update
    that triggered this lookback, and an older confirmation at the same
    company belongs to an earlier, separate application.

    While scanning, an email that communicates an outcome (rejection,
    interview/assessment invite, or offer) is checked against the role being
    searched with _outcome_role_matches, which is deliberately stricter than
    the tolerant _role_matches used for confirmations. One that matches ends
    the scan: a matching outcome older than the update that triggered this
    lookback means the group's own confirmation lies between the two and was
    not found by the primary search, so continuing further back could only
    surface a different application's confirmation. One that does NOT match
    concerns a different application at the same company and is skipped,
    leaving the scan to continue.

    A confirmation candidate whose subject and body both name no role at
    all (_extract_role finds nothing in either) is a WEAK match: it is
    recorded but scanning continues, since a later candidate might still
    yield an explicit, stronger role match. Before recording it, identifiers
    are checked one last time: if the sought side and the candidate both
    carry one and they differ, the candidate is rejected outright rather
    than recorded as weak - a differing requisition number is concrete
    evidence of a different application even though no role phrase was
    found.

    Whenever the scan terminates - by exhausting eligible candidates, by
    hitting max_candidates, or by stopping early on a matching outcome - the
    most recent recorded weak match is returned if one exists, instead of
    found=False. A role-less confirmation subject (e.g. "Thank you for
    applying to Twitch") is common and still usable, just less certain than
    an explicit role match.

    An outcome candidate that carries no identifying information at all on
    either side - no requisition/job identifier, no extractable role text -
    is treated as the SAME application (see _no_identifying_info) and also
    stops the scan: two unidentifiable emails at the same company, adjacent
    in time, are more likely the same application than different ones.
    """
    before_dt = before_date if isinstance(before_date, datetime) else _parse_date(before_date)

    eligible = [c for c in candidates if c["date"] < before_dt]
    eligible.sort(key=lambda c: c["date"], reverse=True)

    weak_match = None

    def _end_of_scan():
        if weak_match is not None:
            print(
                f"[find_application_date] using weak match {weak_match['email_id']} "
                f"(no role identified)"
            )
            return weak_match
        print(f"[find_application_date] no match within {max_candidates} candidates")
        return {"found": False, "date": "", "email_id": ""}

    for candidate in eligible[:max_candidates]:
        date_str = candidate["date"].strftime("%Y-%m-%d")
        candidate_id = candidate["id"]
        subject = candidate.get("subject", "")
        prefix = f'[find_application_date] candidate {date_str} {candidate_id} "{subject}"'

        subject_intent = _classify_intent(subject, "")
        if not subject_intent["application_related"]:
            print(f"{prefix} -> skipped: not application-related")
            continue

        body = candidate.get("body")
        if body is None:
            detail = get_email_detail(candidate_id)
            body = detail["email"].get("body", "")

        intent = _classify_intent(subject, body)
        if not intent["application_related"]:
            print(f"{prefix} -> skipped: not application-related")
            continue

        if intent["communicates_outcome"]:
            if _no_identifying_info(subject, body, role, source_text):
                print(
                    f"{prefix} -> outcome, no identifying information, assuming same "
                    f"application, stopping search"
                )
                return _end_of_scan()
            if _outcome_role_matches(subject, body, role, source_text):
                print(
                    f"{prefix} -> skipped: communicates an outcome "
                    f"(rejection/interview/offer), stopping search"
                )
                return _end_of_scan()
            print(f"{prefix} -> skipped: outcome for a different role")
            continue

        if intent["feedback_only"]:
            print(f"{prefix} -> skipped: feedback/survey request, not a confirmation")
            continue

        if _role_matches(subject, body, role, source_text):
            print(f"{prefix} -> matched")
            return {"found": True, "date": date_str, "email_id": candidate_id}

        candidate_role = _extract_role(subject) or _extract_role(body)
        if not candidate_role:
            sought_ids = _extract_identifiers(role) | _extract_identifiers(source_text)
            candidate_ids = _extract_identifiers(subject) | _extract_identifiers(body)
            if sought_ids and candidate_ids and not (sought_ids & candidate_ids):
                candidate_req = ", ".join(sorted(candidate_ids))
                sought_req = ", ".join(sorted(sought_ids))
                print(
                    f"{prefix} -> skipped: requisition mismatch ({candidate_req} vs {sought_req})"
                )
                continue
            print(f"{prefix} -> weak match (no role identified in email)")
            if weak_match is None:
                weak_match = {"found": True, "date": date_str, "email_id": candidate_id}
            continue

        print(f"{prefix} -> skipped: role mismatch ({_role_hint(subject, body)} vs {role})")

    return _end_of_scan()


def _fetch_candidates(service, query: str, max_results: int) -> list:
    """Runs a Gmail search and returns candidate dicts (id, date, subject)
    for each result with a parseable Date header."""
    response = (
        service.users()
        .messages()
        .list(
            userId="me",
            q=query,
            maxResults=max_results,
        )
        .execute()
    )

    candidates = []
    for msg in response.get("messages", []):
        meta = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in meta["payload"]["headers"]}
        parsed = _parse_header_date(headers.get("Date", ""))
        if parsed is None:
            continue
        candidates.append(
            {
                "id": msg["id"],
                "date": parsed,
                "subject": headers.get("Subject", ""),
            }
        )
    return candidates


def find_application_date(company: str, role: str, before_date: str, source_text: str = "") -> dict:
    """Searches backwards for the application confirmation email for a job,
    when no confirmation was found in the primary search range.

    Args:
        company: Company name as it appears in the emails.
        role: Job title as it appears in the emails.
        before_date: YYYY-MM-DD. The earliest date already known for this
            application. The search covers the period before this date.
        source_text: optional subject+body text of the email(s) that
            triggered this lookback (the entry's own group). Used only for
            requisition/job identifier extraction on the sought side, passed
            through to _select_confirmation - see _role_matches and
            _outcome_role_matches for how a shared or conflicting identifier
            decides a match.

    When an identifier can be extracted from role or source_text (see
    _sought_identifier), a Gmail query for that identifier alone runs
    first: the confirmation's subject typically contains it verbatim, so
    this finds the exact email directly instead of scanning chronologically
    through every email from the company. Those results get the higher
    _MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER cap, since a query already scoped
    to one identifier is cheap to examine more deeply. Only when no
    identifier is available, or the identifier query finds no confirmation,
    does this fall back to the broader company-token query (capped at
    _MAX_CANDIDATES_EXAMINED).

    Returns:
        A dict with keys 'found' (bool), 'date' (YYYY-MM-DD or ""), and
        'email_id' (str or ""). When found is False the caller should keep
        the approximate date it already has.
    """
    try:
        range_after, range_before = get_last_search_range()

        if range_after and range_before:
            range_days = (_parse_date(range_before) - _parse_date(range_after)).days
            lookback_days = max(_MIN_LOOKBACK_DAYS, range_days)
        else:
            lookback_days = _MIN_LOOKBACK_DAYS
        lookback_days = min(lookback_days, _MAX_LOOKBACK_DAYS)

        before_dt = _parse_date(before_date)
        after_dt = before_dt - timedelta(days=lookback_days)

        after_str = after_dt.strftime("%Y-%m-%d")
        before_str = before_dt.strftime("%Y-%m-%d")

        print(f"[find_application_date] {company} / {role} searching {after_str} to {before_str}")

        date_bounds = (
            f"after:{after_dt.strftime('%Y/%m/%d')} before:{before_dt.strftime('%Y/%m/%d')}"
        )
        service = get_gmail_service()

        identifier = _sought_identifier(role, source_text)
        if identifier:
            print(f"[find_application_date] querying by requisition {identifier}")
            candidates = _fetch_candidates(
                service, f"{identifier} {date_bounds}", _MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER
            )
            result = _select_confirmation(
                candidates, role, before_dt, source_text, _MAX_CANDIDATES_EXAMINED_BY_IDENTIFIER
            )
            if result["found"]:
                return result

        token = _company_token(company)
        print(f"[find_application_date] querying by company {token}")
        query_parts = []
        if token:
            query_parts.append(f"(from:{token} OR subject:{token})")
        query_parts.append(date_bounds)
        query = " ".join(query_parts)
        candidates = _fetch_candidates(service, query, _MAX_CANDIDATES_EXAMINED)
        return _select_confirmation(
            candidates, role, before_dt, source_text, _MAX_CANDIDATES_EXAMINED
        )

    except Exception as e:
        print(f"[find_application_date] ERROR {e}")
        return {"found": False, "date": "", "email_id": ""}


def _parse_header_date(date_header: str):
    try:
        parsed = parsedate_to_datetime(date_header)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed
