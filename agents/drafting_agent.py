from google.adk.agents import Agent

from tools.draft_reply import draft_reply

drafting_agent = Agent(
    name="drafting_agent",
    model="gemini-2.5-flash",
    instruction=(
        "You are a specialist agent responsible for drafting email replies. "
        "You compose professional replies based on the original email and user intent. "
        "Always remind the user to review the draft before sending. "
        "Never send emails directly."
    ),
    tools=[draft_reply],
)
