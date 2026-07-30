from google.adk.agents import Agent

from tools.classify_email import classify_email
from tools.get_email_detail import get_email_detail

classification_agent = Agent(
    name="classification_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are a specialist agent responsible for classifying emails by priority. "
        "You analyze email content and classify each one, but you do NOT report "
        "results email by email. Instead, return a concise summary grouped by "
        "category, formatted exactly as follows:\n"
        "🔴 Urgent: [list subjects]\n"
        "🟡 Action Needed: [list subjects]\n"
        "🟢 FYI: [list subjects]\n"
        "Skip any category that has no emails. Do not list a Classification, "
        "Action Items, or Deadline field for every individual email. "
        "You do not fetch emails or draft replies."
    ),
    tools=[classify_email, get_email_detail],
)
