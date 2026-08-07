import base64
import html
import re

from auth import get_thread_local_gmail_service

_MAX_BODY_LENGTH = 2000

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")

_FETCHED_IDS: set[str] = set()
_BODY_CACHE: dict[str, dict] = {}


def get_fetched_ids() -> set:
    """Returns the set of email ids fetched since the last reset."""
    return set(_FETCHED_IDS)


def reset_fetched_ids() -> None:
    """Clears the fetch record and the body cache."""
    _FETCHED_IDS.clear()
    _BODY_CACHE.clear()


def get_email_detail(email_id: str) -> dict:
    """Gets the full content of a specific email by its Gmail message ID.

    Args:
        email_id: The Gmail message ID of the email to retrieve.

    Returns:
        A dict with key 'email' containing id, from, to, subject, date,
        and body (plain text).
    """
    return _fetch_one(email_id, quiet=False)


def _fetch_one(email_id: str, quiet: bool = False) -> dict:
    """Core single-email fetch/cache logic shared by get_email_detail and
    get_emails_bulk. When quiet is True, the routine per-email progress
    lines are suppressed - get_emails_bulk prints one aggregate summary for
    the whole batch instead. The empty-body WARNING still prints regardless,
    since it flags a real data problem rather than routine progress."""
    if email_id in _BODY_CACHE:
        if not quiet:
            print(f"[get_email_detail] {email_id} (cached)")
        _FETCHED_IDS.add(email_id)
        return _BODY_CACHE[email_id]

    service = get_thread_local_gmail_service()

    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=email_id,
            format="full",
        )
        .execute()
    )

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    body = _extract_body(msg["payload"])
    if not body:
        print(
            f"[get_email_detail] WARNING empty body for id {email_id} "
            f"subject {headers.get('Subject', '')}"
        )
    if len(body) > _MAX_BODY_LENGTH:
        body = body[:_MAX_BODY_LENGTH] + "...[truncated]"

    if not quiet:
        print(f"[get_email_detail] {email_id} {headers.get('Subject', '')} {len(body)}")

    _FETCHED_IDS.add(email_id)

    result = {
        "email": {
            "id": email_id,
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body,
        }
    }
    _BODY_CACHE[email_id] = result
    return result


def _find_part_data(payload: dict, mime_type: str) -> str | None:
    """Recursively searches the entire MIME tree for a part with the given
    mimeType, at any depth, and returns its raw body data if present."""
    if payload.get("mimeType", "") == mime_type:
        data = payload.get("body", {}).get("data", "")
        if data:
            return data

    for part in payload.get("parts", []):
        result = _find_part_data(part, mime_type)
        if result:
            return result

    return None


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _html_to_text(raw_html: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("", raw_html)
    text = _TAG_RE.sub("\n", text)
    text = html.unescape(text)
    text = _HORIZONTAL_WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES_RE.sub("\n", text)
    return text.strip()


def _extract_body(payload: dict) -> str:
    """Recursively extracts a readable body from a Gmail message payload.

    Prefers text/plain found anywhere in the MIME tree; falls back to
    text/html (converted to readable text) if no text/plain part exists.
    """
    data = _find_part_data(payload, "text/plain")
    if data:
        return _decode(data)

    data = _find_part_data(payload, "text/html")
    if data:
        return _html_to_text(_decode(data))

    return ""
