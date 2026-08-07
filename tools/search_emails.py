from auth import get_gmail_service


def search_emails(query: str, max_results: int = 20) -> dict:
    """Searches Gmail inbox using a query string.

    Args:
        query: Gmail search query e.g. "from:boss@company.com" or
               "subject:invoice after:2026/01/01".
        max_results: Maximum number of emails to return (default 20).

    Returns:
        A dict with key 'emails' containing a list of email summaries,
        each with id, from, subject, and date.
    """
    print(f"[search_emails] query: {query}")

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
    emails = []

    if response.get("nextPageToken"):
        print(
            f"[search_emails] WARNING more results exist beyond max_results "
            f"({len(messages)} returned)"
        )

    for msg in messages:
        msg_data = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg_data["payload"]["headers"]}
        emails.append(
            {
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }
        )

    print(f"[search_emails] returning {len(emails)} results")

    return {"emails": emails}
