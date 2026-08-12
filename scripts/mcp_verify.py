"""Proves the deployed google_workspace_mcp server is reachable, that IAM
auth works, and that every tool this project calls is actually exposed.

Connects to MCP_SERVER_URL over streamable-http with a Google identity
token (same auth path tools/mcp_client.py uses), runs `initialize` +
`tools/list`, and checks the advertised tool names against the ones the
client's dispatch table maps to. Read-only and safe to run any time; it
never invokes a tool.

Usage:
    MCP_SERVER_URL=https://<service>.run.app python scripts/mcp_verify.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import (  # noqa: E402
    create_mcp_http_client,
    streamable_http_client,
)

from tools.mcp_client import (  # noqa: E402
    _audience,
    _endpoint_url,
    _GoogleIDTokenAuth,
)

_REQUIRED_PRESENT = (
    "get_gmail_message_content",
    "get_gmail_messages_content_batch",
    "search_gmail_messages",
    "read_sheet_values",
)


async def _list_tools() -> list[str]:
    endpoint = _endpoint_url()
    http_client = create_mcp_http_client(auth=_GoogleIDTokenAuth(_audience()))
    try:
        async with streamable_http_client(endpoint, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [tool.name for tool in result.tools]
    finally:
        await http_client.aclose()


def main() -> int:
    try:
        names = asyncio.run(_list_tools())
    except Exception as e:
        print(f"[mcp_verify] FAILED to list tools: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[mcp_verify] server advertises {len(names)} tools:")
    for name in sorted(names):
        print(f"  {name}")

    missing = [name for name in _REQUIRED_PRESENT if name not in names]
    if missing:
        print(f"[mcp_verify] FAILED: expected tools missing: {missing}", file=sys.stderr)
        return 1

    print("[mcp_verify] PASSED: all tools this project calls are exposed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
