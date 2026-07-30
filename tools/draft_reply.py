import os

import google.genai as genai


def draft_reply(email: dict, intent: str) -> dict:
    """Drafts a reply to an email for the user to review before sending.

    Uses Gemini to generate a professional reply based on the original email
    and the user's stated intent. The draft is returned for review — nothing
    is sent automatically.

    Args:
        email: A dict with keys 'from', 'subject', 'body' (or 'snippet').
        intent: A short description of what the user wants to communicate,
                e.g. "accept the meeting", "ask for more time".

    Returns:
        A dict with key 'draft' containing the suggested reply body text.
    """
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    body_text = email.get("body") or email.get("snippet", "")

    prompt = f"""Write a professional email reply.

Original email:
From: {email.get('from', '')}
Subject: {email.get('subject', '')}
Body: {body_text}

User's intent for the reply: {intent}

Write only the email body text. Do not include a subject line, headers, or
any explanation. The user will review this draft before deciding to send it."""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )

    return {"draft": response.text.strip()}
