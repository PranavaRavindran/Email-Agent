from auth import get_gmail_service


def list_emails(max_results: int = 100) -> dict:
    """Fetches the latest emails from the user's Gmail inbox.

    Args:
        max_results: Maximum number of emails to return (default 100).

    Returns:
        A dict with key 'emails' containing a list of email summaries,
        each with id, from, subject, date, and snippet.
    """
    service = get_gmail_service()

    response = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()

    messages = response.get("messages", [])
    emails = []

    if response.get("nextPageToken"):
        print(f"[list_emails] WARNING more results exist beyond max_results ({len(messages)} returned)")

    for msg in messages:
        msg_data = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

        headers = {
            h["name"]: h["value"]
            for h in msg_data["payload"]["headers"]
        }
        emails.append({
            "id": msg["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": msg_data.get("snippet", ""),
        })

    return {"emails": emails}
