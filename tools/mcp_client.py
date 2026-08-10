import asyncio
import atexit
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

_DEFAULT_WORKSPACE_MCP_DIR = os.path.expanduser("~/.workspace-mcp")

_WORKSPACE_FEATURE_OVERRIDES = (
    "docs.read:off,docs.write:off,drive.read:off,drive.write:off,"
    "calendar.read:off,calendar.write:off,chat.read:off,chat.write:off,"
    "gmail.write:off,people.read:off,slides.read:off"
)

_session: ClientSession | None = None
_session_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
# Kept alive across the session's lifetime so _shutdown can __aexit__ them in
# the reverse order they were entered.
_session_cm: Any = None
_stdio_cm: Any = None


class MCPClientError(RuntimeError):
    """Raised when the MCP server process or session cannot be used."""


def mcp_call(tool_name: str, arguments: dict) -> dict:
    """Calls an MCP tool synchronously and returns its parsed JSON result.

    Spawns and initializes the server session on first call (thread-safe,
    double-checked locking, same pattern as auth.py's get_gmail_service).
    Safe to call concurrently from multiple threads - the client session
    supports concurrent in-flight requests, so calls are not serialized.

    Raises:
        MCPClientError: if the server can't be spawned/initialized, the tool
            call itself errors, or the response has no text content to parse.
    """
    _ensure_session()
    assert _loop is not None
    future = asyncio.run_coroutine_threadsafe(_call(tool_name, arguments), _loop)
    try:
        return future.result()
    except MCPClientError:
        raise
    except Exception as e:
        raise MCPClientError(f"MCP call '{tool_name}' failed: {type(e).__name__}: {e}") from e


async def _call(tool_name: str, arguments: dict) -> dict:
    assert _session is not None
    result = await _session.call_tool(tool_name, arguments)
    text = _extract_text(result)
    if result.is_error:
        raise MCPClientError(f"MCP tool '{tool_name}' returned an error: {text}")
    return json.loads(text)


def _extract_text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise MCPClientError(f"MCP tool result had no text content: {result.content!r}")


def _ensure_session() -> None:
    if _session is None:
        with _session_lock:
            if _session is None:
                _start()


def _workspace_mcp_dir() -> Path:
    return Path(os.environ.get("WORKSPACE_MCP_DIR", _DEFAULT_WORKSPACE_MCP_DIR)).expanduser()


def _server_entrypoint() -> Path:
    entrypoint = _workspace_mcp_dir() / "workspace-server" / "dist" / "index.js"
    if not entrypoint.exists():
        raise MCPClientError(
            f"MCP server build not found at {entrypoint}. Build it first:\n"
            f"  cd {entrypoint.parent.parent} && npm install && npm run build"
        )
    return entrypoint


def _check_node() -> str:
    node = shutil.which("node")
    if node is None:
        raise MCPClientError(
            "node executable not found on PATH. The Google Workspace MCP server "
            "requires Node.js >= 20."
        )
    return node


def _start() -> None:
    global _loop, _loop_thread

    ready = threading.Event()
    errors: list[BaseException] = []
    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_connect(ready, errors))
        except BaseException as e:  # pragma: no cover - _connect already catches
            errors.append(e)
            ready.set()
            return
        if not errors:
            loop.run_forever()
        loop.close()

    thread = threading.Thread(target=runner, name="mcp-client-loop", daemon=True)
    thread.start()
    ready.wait()

    if errors:
        thread.join(timeout=2)
        raise MCPClientError(f"Failed to start MCP client session: {errors[0]}") from errors[0]

    _loop = loop
    _loop_thread = thread
    atexit.register(_shutdown)


async def _connect(ready: threading.Event, errors: list[BaseException]) -> None:
    global _session, _session_cm, _stdio_cm
    try:
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
        stdio_cm = stdio_client(params)
        read_stream, write_stream = await stdio_cm.__aenter__()

        session_cm = ClientSession(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()

        _stdio_cm = stdio_cm
        _session_cm = session_cm
        _session = session
    except BaseException as e:
        errors.append(e)
    finally:
        ready.set()


def _shutdown() -> None:
    global _session, _session_cm, _stdio_cm, _loop, _loop_thread
    loop = _loop
    if loop is None or loop.is_closed():
        return

    future = asyncio.run_coroutine_threadsafe(_teardown(), loop)
    try:
        future.result(timeout=10)
    except Exception:
        pass

    loop.call_soon_threadsafe(loop.stop)
    if _loop_thread is not None:
        _loop_thread.join(timeout=5)

    _session = None
    _session_cm = None
    _stdio_cm = None
    _loop = None
    _loop_thread = None


async def _teardown() -> None:
    # Exits in the reverse order they were entered: the SDK's stdio_client
    # teardown SIGTERMs (then SIGKILLs) the server's whole process group, so
    # this needs to run before the loop stops.
    if _session_cm is not None:
        await _session_cm.__aexit__(None, None, None)
    if _stdio_cm is not None:
        await _stdio_cm.__aexit__(None, None, None)
