import json
import os

import google.genai as genai
from google.genai import types


def classify_email(email: dict) -> dict:
    """Classifies an email by priority and extracts action items and deadlines.

    Uses Gemini to analyze the email and return a structured classification.

    Args:
        email: A dict with keys 'from', 'subject', 'body' (or 'snippet'),
               and optionally 'date'.

    Returns:
        A dict with:
          - classification: one of "urgent", "action_needed", "fyi", or "spam"
          - action_items: list of specific actions required (may be empty)
          - deadline: deadline string if mentioned, else empty string
    """
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    body_text = email.get("body") or email.get("snippet", "")

    prompt = f"""Analyze the following email and classify it.

From: {email.get('from', '')}
Subject: {email.get('subject', '')}
Body: {body_text}

Respond with a JSON object containing exactly these keys:
- "classification": one of "urgent", "action_needed", "fyi", or "spam"
- "action_items": a JSON array of strings listing specific actions required (empty array if none)
- "deadline": deadline mentioned in the email as a string (empty string if none)

Classification criteria:
- "urgent": security alerts, such as a new sign-in, password reset, or account
  compromise warning for one of the user's accounts.
- "action_needed": a recruiter email that asks the user to schedule a call or
  respond by a specific date; or any email containing a specific deadline that
  is addressed to the user personally (by name or in a way that makes clear it
  is directed at them individually, not a mass send).
- "fyi": informational only, including job rejections, job recommendations,
  marketing emails, general recruiter outreach with no scheduling ask or
  deadline, and job board notifications.
- "spam": unsolicited or malicious content with no legitimate informational value.

Marketing emails, job recommendations, and job rejections are NOT
action_needed — classify them as fyi even if they contain calls to action like
"apply now" or "view jobs".

Examples:
- A job board digest listing new postings ("5 new jobs match your search") -> fyi
- A security alert about a new sign-in to the user's account -> urgent
- A "thanks for applying, we've decided to move forward with other candidates" email -> fyi
- A recruiter's mass outreach message sent to many candidates about an open role, with no scheduling ask -> fyi
- A recruiter emailing the user directly asking them to pick a time for a call, or to respond by a given date -> action_needed
- An email from the user's direct manager asking them by name to do something by a specific date -> action_needed

Respond ONLY with the JSON object, no markdown fences or extra text."""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    try:
        result = json.loads(response.text.strip())
    except (json.JSONDecodeError, AttributeError):
        result = {
            "classification": "fyi",
            "action_items": [],
            "deadline": "",
        }

    return {
        "classification": result.get("classification", "fyi"),
        "action_items": result.get("action_items", []),
        "deadline": result.get("deadline", ""),
    }
