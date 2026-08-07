import re

from auth import get_gmail_service
from tools.get_email_detail import reset_fetched_ids

_LAST_SEARCH_COUNT = 0
_LAST_SEARCH_AFTER = ""
_LAST_SEARCH_BEFORE = ""

_AFTER_RE = re.compile(r"after:(\d{4})/(\d{2})/(\d{2})")
_BEFORE_RE = re.compile(r"before:(\d{4})/(\d{2})/(\d{2})")


def get_last_search_count() -> int:
    """Returns the number of ids returned by the most recent search."""
    return _LAST_SEARCH_COUNT


def get_last_search_range() -> tuple:
    """Returns (after, before) as YYYY-MM-DD strings from the most recent
    search query, or ("", "") if the query had no date bounds."""
    return (_LAST_SEARCH_AFTER, _LAST_SEARCH_BEFORE)


def search_email_ids(query: str, max_results: int = 100) -> dict:
    """Searches Gmail inbox using a query string, returning ids only.

    Args:
        query: Gmail search query e.g. "from:boss@company.com" or
               "subject:invoice after:2026/01/01".
        max_results: Maximum number of emails to return (default 100).

    Returns:
        A dict with key 'emails' containing a list of dicts, each with
        only an 'id' key. No subject, from, date, or snippet is included.
    """
    print(f"[search_email_ids] query: {query}")

    global _LAST_SEARCH_AFTER, _LAST_SEARCH_BEFORE
    after_match = _AFTER_RE.search(query)
    before_match = _BEFORE_RE.search(query)
    _LAST_SEARCH_AFTER = "-".join(after_match.groups()) if after_match else ""
    _LAST_SEARCH_BEFORE = "-".join(before_match.groups()) if before_match else ""

    reset_fetched_ids()

    service = get_gmail_service()

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

    messages = response.get("messages", [])
    emails = [{"id": msg["id"]} for msg in messages]

    if response.get("nextPageToken"):
        print(
            f"[search_email_ids] WARNING more results exist beyond max_results "
            f"({len(emails)} returned)"
        )

    print(f"[search_email_ids] returning {len(emails)} ids")

    global _LAST_SEARCH_COUNT
    _LAST_SEARCH_COUNT = len(emails)

    return {"emails": emails}
