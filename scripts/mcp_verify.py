"""Proves the Google Workspace MCP server's tool allowlist took effect
server-side, without needing OAuth consent.

Spawns the server directly (same command tools/mcp_client.py uses), runs
`initialize` + `tools/list`, and checks the surviving tool names against
what WORKSPACE_FEATURE_OVERRIDES is supposed to produce: gmail_get and the
sheets read tools present; gmail_send/gmail_modify/docs_*/drive_*/
calendar_*/chat_*/people_* absent. `tools/list` requires no auth - unlike
scripts/mcp_smoke.py, this never opens a browser or touches OAuth, and is
safe to run any time.

Usage: python scripts/mcp_verify.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402

from tools.mcp_client import (  # noqa: E402
    _WORKSPACE_FEATURE_OVERRIDES,
    _check_node,
    _server_entrypoint,
)

_REQUIRED_PRESENT = ("gmail_get", "sheets_getRange", "sheets_getText", "sheets_getMetadata")
_FORBIDDEN_EXACT = ("gmail_send", "gmail_modify")
_FORBIDDEN_PREFIXES = ("docs_", "drive_", "calendar_", "chat_", "people_")


async def _list_tools() -> list[str]:
    node = _check_node()
    entrypoint = _server_entrypoint()
    server_dir = entrypoint.parent.parent

    env = dict(os.environ)
    env["WORKSPACE_FEATURE_OVERRIDES"] = _WORKSPACE_FEATURE_OVERRIDES

    params = StdioServerParameters(
        command=node,
        args=[str(entrypoint)],
        env=env,
        cwd=str(server_dir),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.list_tools()
            return [tool.name for tool in result.tools]


def main() -> int:
    try:
        names = asyncio.run(_list_tools())
    except Exception as e:
        print(f"[mcp_verify] FAILED to list tools: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[mcp_verify] {len(names)} tools survived the allowlist:")
    for name in sorted(names):
        print(f"  {name}")

    missing = [name for name in _REQUIRED_PRESENT if name not in names]
    forbidden_present = [name for name in names if name in _FORBIDDEN_EXACT] + [
        name for name in names if name.startswith(_FORBIDDEN_PREFIXES)
    ]

    ok = True
    if missing:
        print(f"[mcp_verify] FAILED: expected tools missing: {missing}", file=sys.stderr)
        ok = False
    if forbidden_present:
        print(
            f"[mcp_verify] FAILED: disallowed tools present: {forbidden_present}", file=sys.stderr
        )
        ok = False

    if ok:
        print("[mcp_verify] PASSED: allowlist took effect server-side.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
