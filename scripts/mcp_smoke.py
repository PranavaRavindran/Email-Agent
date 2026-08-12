"""One-time, user-run smoke test for the MCP gmail_get and sheets_getRange
paths against the deployed Cloud Run server.

No browser or OAuth consent is involved anymore: the server holds its own
Google credentials, and this client authenticates to Cloud Run with an IAM
identity token minted from Application Default Credentials. The requests DO
read real Gmail/Sheets data, so run it deliberately, not from automation.

Usage:
    MCP_SERVER_URL=https://<service>.run.app \\
    USER_GOOGLE_EMAIL=<account the server is authorized for> \\
    python scripts/mcp_smoke.py <gmail_message_id>

Grab a message id via the existing raw path first, e.g.:
    python -c "from tools.search_email_ids import search_email_ids; \
print(search_email_ids('is:unread', max_results=1))"
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.mcp_client import mcp_call  # noqa: E402
from tools.write_to_sheet import _READ_RANGE, _SPREADSHEET_ID  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/mcp_smoke.py <gmail_message_id>", file=sys.stderr)
        return 1
    message_id = sys.argv[1]

    print(f"[mcp_smoke] calling gmail_get for message {message_id}...")
    gmail_result = mcp_call("gmail_get", {"messageId": message_id})
    print(json.dumps(gmail_result, indent=2)[:2000])

    print(f"[mcp_smoke] calling sheets_getRange for {_SPREADSHEET_ID} {_READ_RANGE}...")
    sheets_result = mcp_call(
        "sheets_getRange", {"spreadsheetId": _SPREADSHEET_ID, "range": _READ_RANGE}
    )
    print(json.dumps(sheets_result, indent=2)[:2000])

    print("[mcp_smoke] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
