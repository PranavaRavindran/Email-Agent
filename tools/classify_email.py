import concurrent.futures
import json

from google.genai import types

from tools.genai_client import get_genai_client

_MAX_WORKERS = 8


def _classify_one(email: dict) -> dict:
    """Classifies a single email; the prompt, model call, parsing and
    safe-default fallback are the original single-email tool moved verbatim.

    Never raises: any failure returns the safe default
    ("fyi", no action items, no deadline).
    """
    subject = email.get("subject", "")
    print(f"[classify_email] {subject[:60]}")

    client = get_genai_client()

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


def classify_emails(emails: list[dict]) -> dict:
    """Classifies a batch of emails by priority in a single call.

    Pass ALL emails to classify in one invocation — do not call this tool
    once per email. Each email is classified independently and concurrently
    against the exact email content provided; nothing is fetched or
    re-fetched.

    Args:
        emails: A list of dicts, each with keys 'from', 'subject',
            'body' (or 'snippet'), and optionally 'date'.

    Returns:
        A dict with a "results" list containing exactly one entry per input
        email, in input order. Each entry has:
          - index: position of the email in the input list
          - subject: the input email's subject, echoed back
          - classification: one of "urgent", "action_needed", "fyi", or "spam"
          - action_items: list of specific actions required (may be empty)
          - deadline: deadline string if mentioned, else empty string
        An email whose classification fails gets the safe default ("fyi",
        no action items, no deadline); the rest of the batch is unaffected.
    """
    print(f"[classify_emails] classifying {len(emails)} emails")
    if not emails:
        return {"results": []}

    # Pre-filled with safe defaults so every input has a result even if a
    # worker fails in a way the helper's own fallback cannot catch.
    classifications: list[dict] = [
        {"classification": "fyi", "action_items": [], "deadline": ""} for _ in emails
    ]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_MAX_WORKERS, len(emails))
    ) as executor:
        future_to_index = {
            executor.submit(_classify_one, email): i for i, email in enumerate(emails)
        }
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                classifications[i] = future.result()
            except Exception as e:
                print(f"[classify_emails] ERROR {type(e).__name__}: {e}")

    return {
        "results": [
            {"index": i, "subject": emails[i].get("subject", ""), **classification}
            for i, classification in enumerate(classifications)
        ]
    }
