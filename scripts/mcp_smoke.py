"""One-time, user-run smoke test for the MCP gmail_get and sheets_getRange
paths. REQUIRES GOOGLE OAUTH CONSENT - an agent must never run this; only a
human watching the browser should.

First run opens a browser for the MCP server's own OAuth consent (separate
from this project's credentials.json/token.pickle - see MCP_INTEGRATION.md's
dual-auth model). The consent screen must show ONLY read-only access to
Gmail and Sheets. If it asks for write/send access, or for Docs, Drive,
Calendar, Chat, or People, ABORT the consent flow and do not approve - it
means WORKSPACE_FEATURE_OVERRIDES (tools/mcp_client.py) isn't narrowing the
scope request the way MCP_INTEGRATION.md says it should.

Usage: python scripts/mcp_smoke.py <gmail_message_id>

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

_BANNER = """
============================================================
 MCP SMOKE TEST - this will open a browser for Google OAuth
 consent on first run (separate from this project's own
 credentials.json).

 On the consent screen, verify it asks for ONLY:
   - Gmail: read-only access
   - Sheets: read-only access
 If you see write/send access, or Docs/Drive/Calendar/Chat/
 People access, DO NOT APPROVE - abort and check
 WORKSPACE_FEATURE_OVERRIDES in tools/mcp_client.py instead.
============================================================
"""


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/mcp_smoke.py <gmail_message_id>", file=sys.stderr)
        return 1
    message_id = sys.argv[1]

    print(_BANNER)
    input("Press Enter to continue (Ctrl+C to abort)... ")

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
