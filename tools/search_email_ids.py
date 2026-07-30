from auth import get_gmail_service


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

    service = get_gmail_service()

    response = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results,
    ).execute()

    messages = response.get("messages", [])
    emails = [{"id": msg["id"]} for msg in messages]

    if response.get("nextPageToken"):
        print(f"[search_email_ids] WARNING more results exist beyond max_results ({len(emails)} returned)")

    print(f"[search_email_ids] returning {len(emails)} ids")

    return {"emails": emails}
