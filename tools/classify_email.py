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
    subject = email.get("subject", "")
    print(f"[classify_email] {subject[:60]}")

    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

    body_text = email.get("body") or email.get("snippet", "")

    prompt = f"""Analyze the following email and classify it.

From: {email.get("from", "")}
Subject: {email.get("subject", "")}
Body: {body_text}

Respond with a JSON object containing exactly these keys:
- "classification": one of "urgent", "action_needed", "fyi", or "spam"
- "action_items": a JSON array of strings listing specific actions required (empty array if none)
- "deadline": deadline mentioned in the email as a string (empty string if none)

Governing principle: this is a job-application assistant, and "urgent" means
the user's JOB SEARCH needs them now — a deadline or a required action in
their application process. General time-sensitivity (an item that expires
soon) is NOT sufficient on its own to make something urgent. An email can
feel time-pressured — a code about to expire, a security alert — while
requiring zero job-search action from the user; that email is fyi, not
urgent.

Classification criteria:
- "urgent": the user's job search needs action now. This includes: interview
  scheduling requests; assessments or take-home exercises with a due date; a
  recruiter asking the user to confirm a time or respond by a given date; an
  application requiring additional information by a deadline.
- "action_needed": wants a response from the user but is not time-boxed —
  direct recruiter outreach addressed to the user personally, or a form or
  profile step tied to a live application, with no stated deadline.
- "fyi": everything else, including things that look time-sensitive but
  require no job-search action from the user: verification codes (the user
  triggered the code themselves and is already looking for it, even if it
  expires soon); security alerts such as a new sign-in, password reset, or
  account compromise warning; job rejections; application status updates
  that require no action; job recommendations, job board digests, marketing
  emails, and receipts.
- "spam": unsolicited or malicious content with no legitimate informational value.

Marketing emails, job recommendations, and job rejections are NOT
action_needed — classify them as fyi even if they contain calls to action like
"apply now" or "view jobs". Verification codes and security alerts are NOT
urgent, even when time-sensitive — classify them as fyi.

Examples:
- A job board digest listing new postings ("5 new jobs match your search") -> fyi
- A security alert about a new sign-in to the user's account -> fyi (no
  job-search action is required, even though it feels urgent)
- A one-time verification code, e.g. "Your TikTok verification code: 840490",
  even one expiring in 30 minutes -> fyi (the user triggered this themselves
  and is already looking for it; expiring soon does not make it job-search
  urgent)
- A "thanks for applying, we've decided to move forward with other candidates" email -> fyi
- An application status update ("your application is under review") with no
  action requested -> fyi
- A recruiter's mass outreach message sent to many candidates about an open \
role, with no scheduling ask -> fyi
- A recruiter emailing the user directly asking them to pick a time for an \
interview, or to respond by a given date -> urgent
- An assessment or take-home exercise invitation with a stated due date -> urgent
- A recruiter emailing the user directly to introduce a role, with no \
scheduling ask or deadline -> action_needed
- A profile-completion or additional-forms request tied to a live \
application, with no stated deadline -> action_needed

Respond ONLY with the JSON object, no markdown fences or extra text."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
    except Exception as e:
        print(f"[classify_email] ERROR {type(e).__name__}: {e}")
        return {
            "classification": "fyi",
            "action_items": [],
            "deadline": "",
        }

    try:
        result = json.loads(response.text.strip())  # type: ignore[union-attr]
    except (json.JSONDecodeError, AttributeError):
        print("[classify_email] ERROR could not parse response")
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
