"""Client for the google_workspace_mcp server deployed on Cloud Run.

Talks streamable-http to the service named by MCP_SERVER_URL. When the URL
is https - a real Cloud Run endpoint - every request carries a Google-signed
identity token whose audience is the service URL (Cloud Run is deployed
--no-allow-unauthenticated; IAM is the only auth boundary). Locally the
token comes from Application Default Credentials; deployed it comes from the
runtime service account - same code path either way. When the URL is http -
a `gcloud run services proxy` listener or a test double - no token is
attached: the proxy adds its own credentials, and plain user-credential ADC
cannot mint identity tokens anyway.

The server's tools return FORMATTED TEXT, not JSON: every tool's Python
signature is `-> str` and `structuredContent` merely duplicates the text
block. This module therefore owns a parser per tool that reconstructs the
dict shape each caller in tools/ already expects, so `mcp_call(tool_name,
arguments) -> dict` keeps its contract and all format brittleness is
quarantined here. A response that does not match the expected shape raises
MCPClientError carrying a snippet of the unparsed text - silent partial
parses are the failure mode this design exists to avoid.

Callers keep passing the tool names and argument spellings they always have
("gmail_get" with messageId, "sheets_getRange" with spreadsheetId/range);
the dispatch table maps them to the new server's tool names and argument
names, and injects the user_google_email argument every server tool
requires (from USER_GOOGLE_EMAIL).
"""

import ast
import asyncio
import atexit
import logging
import os
import re
import threading
from collections.abc import AsyncGenerator, Generator
from typing import Any
from urllib.parse import urlsplit

import httpx2
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

_logger = logging.getLogger(__name__)

_URL_ENV_VAR = "MCP_SERVER_URL"
_EMAIL_ENV_VAR = "USER_GOOGLE_EMAIL"

# The server processes get_gmail_messages_content_batch in chunks of 25
# (GMAIL_BATCH_SIZE in gmail/gmail_tools.py, pinned commit db62129) and
# documents 25 as the per-call maximum, so requests are chunked client-side
# and the parsed results merged - callers can keep asking for any batch size.
_GMAIL_BATCH_LIMIT = 25

# How much of an unparseable response to include in error messages.
_SNIPPET_LEN = 200

_BODY_DELIM = "\n--- BODY ---\n"
_ATTACHMENTS_DELIM = "\n\n--- ATTACHMENTS ---\n"
# The server emits these placeholder strings instead of omitting the line.
_ABSENT_HEADER_SENTINEL = "[not present in Gmail response]"
_EMPTY_BODY_SENTINEL = "[No text/plain body found]"

_HEADER_KEY_TO_FIELD = {
    "Message ID": "id",
    "Subject": "subject",
    "From": "from",
    "Date": "date",
    "To": "to",
    "Cc": "cc",
    "Web Link": "web_link",
}

_BATCH_PREAMBLE_RE = re.compile(r"\ARetrieved (\d+) messages:\n\n")
# Records are joined with "\n---\n\n"; anchoring the lookahead on how every
# record begins keeps a lone "---" line inside an email body from splitting
# that record (the count check below catches anything that slips through).
_BATCH_RECORD_SEP_RE = re.compile(r"\n---\n\n(?=Message ID: |⚠️ Message )")
_BATCH_ERROR_RECORD_RE = re.compile(r"\A⚠️ Message (\S+): (.*)\Z", re.DOTALL)

_SEARCH_HEADER_RE = re.compile(r"\AFound (\d+) messages matching '(.*)':\n")
_SEARCH_ENTRY_RE = re.compile(
    r"^ {2}\d+\. Message ID: (\S+)\n"
    r" {5}Web Link: (\S+)\n"
    r" {5}Thread ID: (\S+)\n"
    r" {5}Thread Link: (\S+)$",
    re.MULTILINE,
)
_SEARCH_PAGE_TOKEN_RE = re.compile(r"page_token='([^']*)'")

_SHEET_HEADER_RE = re.compile(r"\ASuccessfully read (\d+) rows from range '.*?' in spreadsheet ")
_SHEET_ROW_RE = re.compile(r"\ARow +(\d+): (\[.*\])\Z")

_session: ClientSession | None = None
_session_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
# Kept alive across the session's lifetime so _shutdown can close them in
# the reverse order they were opened.
_session_cm: Any = None
_transport_cm: Any = None
_http_client: httpx2.AsyncClient | None = None


class MCPClientError(RuntimeError):
    """Raised when the MCP server can't be reached, a tool call errors, or a
    response doesn't parse into the shape the caller expects."""


def mcp_call(tool_name: str, arguments: dict) -> dict:
    """Calls an MCP tool synchronously and returns its parsed result dict.

    Connects to the server on first call (thread-safe, double-checked
    locking). Safe to call concurrently from multiple threads - the client
    session supports concurrent in-flight requests, so calls are not
    serialized.

    Raises:
        MCPClientError: if MCP_SERVER_URL/USER_GOOGLE_EMAIL are unset, the
            session can't be established, the tool call itself errors, the
            tool name has no registered parser, or the response text does
            not match the tool's expected format.
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        raise MCPClientError(
            f"No parser registered for MCP tool '{tool_name}'; refusing to return "
            f"unparsed text. Known tools: {sorted(_HANDLERS)}"
        )
    return handler(arguments)


# ---------------------------------------------------------------------------
# Per-tool handlers: translate caller arguments, call the server, parse text
# ---------------------------------------------------------------------------


def _require_arg(arguments: dict, key: str, tool_name: str) -> Any:
    value = arguments.get(key)
    if value is None:
        raise MCPClientError(f"MCP tool '{tool_name}' requires argument '{key}'")
    return value


def _handle_gmail_get(arguments: dict) -> dict:
    message_id = _require_arg(arguments, "messageId", "gmail_get")
    text = _call_tool_text(
        "get_gmail_message_content",
        {"message_id": message_id, "user_google_email": _user_google_email()},
    )
    return _parse_message_content(text)


def _handle_batch(arguments: dict) -> dict:
    message_ids = list(_require_arg(arguments, "message_ids", "get_gmail_messages_content_batch"))
    if not message_ids:
        raise MCPClientError("get_gmail_messages_content_batch requires a non-empty 'message_ids'")
    email = _user_google_email()
    messages: list[dict] = []
    for start in range(0, len(message_ids), _GMAIL_BATCH_LIMIT):
        chunk = message_ids[start : start + _GMAIL_BATCH_LIMIT]
        server_args = dict(arguments)
        server_args["message_ids"] = chunk
        server_args["user_google_email"] = email
        text = _call_tool_text("get_gmail_messages_content_batch", server_args)
        messages.extend(_parse_batch_messages(text, chunk))
    return {"messages": messages}


def _handle_search(arguments: dict) -> dict:
    _require_arg(arguments, "query", "search_gmail_messages")
    server_args = dict(arguments)
    server_args["user_google_email"] = _user_google_email()
    text = _call_tool_text("search_gmail_messages", server_args)
    return _parse_search_results(text)


def _handle_sheets_get_range(arguments: dict) -> dict:
    spreadsheet_id = _require_arg(arguments, "spreadsheetId", "sheets_getRange")
    range_name = _require_arg(arguments, "range", "sheets_getRange")
    text = _call_tool_text(
        "read_sheet_values",
        {
            "spreadsheet_id": spreadsheet_id,
            "range_name": range_name,
            "user_google_email": _user_google_email(),
        },
    )
    return _parse_sheet_values(text)


_HANDLERS = {
    # Legacy names the existing call sites pass, mapped to the new server.
    "gmail_get": _handle_gmail_get,
    "sheets_getRange": _handle_sheets_get_range,
    # New-server names, callable directly by future callers.
    "get_gmail_message_content": _handle_gmail_get,
    "get_gmail_messages_content_batch": _handle_batch,
    "search_gmail_messages": _handle_search,
    "read_sheet_values": _handle_sheets_get_range,
}


# ---------------------------------------------------------------------------
# Parsers: formatted text -> the dict shape each tool's callers expect
# ---------------------------------------------------------------------------


def _snippet(text: str) -> str:
    return text[:_SNIPPET_LEN]


def _parse_header_block(block: str, text: str) -> dict:
    """Parses `Key: Value` lines. Values may themselves contain colons
    (Message-ID, Web Link), so each line splits on its first colon only.
    Keys outside _HEADER_KEY_TO_FIELD (List-Unsubscribe, References, ...)
    are ignored; the To/Cc absence sentinel maps to the key being absent."""
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise MCPClientError(
                f"Unparseable header line {line!r} in MCP response: {_snippet(text)!r}"
            )
        field = _HEADER_KEY_TO_FIELD.get(key.strip())
        if field is None:
            continue
        value = value.removeprefix(" ")
        if field in ("to", "cc") and value == _ABSENT_HEADER_SENTINEL:
            continue
        fields[field] = value
    return fields


def _split_body(record: str, text: str) -> tuple[str, str]:
    """Splits a message record into (header block, body) on the FIRST
    occurrence of the body delimiter - a body may legitimately contain the
    literal "--- BODY ---" line, and only the first one is the server's."""
    head, sep, body = record.partition(_BODY_DELIM)
    if not sep:
        raise MCPClientError(
            f"MCP message response has no '--- BODY ---' delimiter: {_snippet(text)!r}"
        )
    # The server appends attachment metadata after the body; it belongs to
    # neither the body nor any field the callers use.
    body = body.split(_ATTACHMENTS_DELIM, 1)[0]
    return head, body


def _parse_message_content(text: str) -> dict:
    """Parses get_gmail_message_content output into the dict shape
    get_email_detail's MCP path has always consumed: subject/from/date/body
    always present, to/cc present only when Gmail returned them."""
    head, body = _split_body(text, text)
    fields = _parse_header_block(head, text)
    for required in ("subject", "from", "date"):
        if required not in fields:
            raise MCPClientError(
                f"MCP message response is missing the '{required}' header: {_snippet(text)!r}"
            )
    if body == _EMPTY_BODY_SENTINEL:
        body = ""
    result = dict(fields)
    result["body"] = body
    return result


def _parse_batch_messages(text: str, expected_ids: list[str]) -> list[dict]:
    """Parses one get_gmail_messages_content_batch (format=full) response
    into a list of per-message dicts, in request order. Failed ids come
    back as {"id", "error"} entries, mirroring the server's per-message
    error lines."""
    preamble = _BATCH_PREAMBLE_RE.match(text)
    if preamble is None:
        raise MCPClientError(f"Unexpected batch response preamble: {_snippet(text)!r}")
    count = int(preamble.group(1))
    if count != len(expected_ids):
        raise MCPClientError(
            f"Batch response reports {count} messages but {len(expected_ids)} were "
            f"requested: {_snippet(text)!r}"
        )

    records = _BATCH_RECORD_SEP_RE.split(text[preamble.end() :])
    if len(records) != count:
        raise MCPClientError(
            f"Batch response contains {len(records)} message records but reports "
            f"{count}: {_snippet(text)!r}"
        )

    messages = []
    for record in records:
        record = record.removesuffix("\n")
        error_match = _BATCH_ERROR_RECORD_RE.match(record)
        if error_match is not None:
            messages.append({"id": error_match.group(1), "error": error_match.group(2)})
            continue
        head, body = _split_body(record, text)
        fields = _parse_header_block(head, text)
        if "id" not in fields:
            raise MCPClientError(
                f"Batch message record has no 'Message ID' line: {_snippet(record)!r}"
            )
        entry = dict(fields)
        # The batch format writes the body as "--- BODY ---\n<body>\n"; the
        # final newline is the format's, not the body's.
        entry["body"] = body.removesuffix("\n")
        messages.append(entry)

    parsed_ids = [message["id"] for message in messages]
    if parsed_ids != list(expected_ids):
        raise MCPClientError(
            f"Batch response ids {parsed_ids} do not match requested ids "
            f"{list(expected_ids)}: {_snippet(text)!r}"
        )
    return messages


def _parse_search_results(text: str) -> dict:
    """Parses search_gmail_messages output into {"messages": [...],
    "next_page_token": str | None}."""
    if text.startswith("No messages found for query:"):
        return {"messages": [], "next_page_token": None}

    header = _SEARCH_HEADER_RE.match(text)
    if header is None:
        raise MCPClientError(f"Unexpected search response format: {_snippet(text)!r}")
    count = int(header.group(1))

    messages = [
        {
            "id": m.group(1),
            "web_link": m.group(2),
            "thread_id": m.group(3),
            "thread_link": m.group(4),
        }
        for m in _SEARCH_ENTRY_RE.finditer(text)
    ]
    if len(messages) != count:
        raise MCPClientError(
            f"Search response reports {count} messages but {len(messages)} entries "
            f"parsed: {_snippet(text)!r}"
        )

    token_match = _SEARCH_PAGE_TOKEN_RE.search(text)
    return {"messages": messages, "next_page_token": token_match.group(1) if token_match else None}


def _parse_sheet_values(text: str) -> dict:
    """Parses read_sheet_values output back into {"values": [[...], ...]},
    the raw Sheets API shape _read_existing_rows has always consumed.

    The server prints each row as a Python list repr ("Row  1: ['a', 'b']"),
    padded with trailing empty strings to the first row's width; the padding
    is stripped so the rows come back exactly as ragged as the raw API path
    returns them. Trailing sections (range-clamp notes, error details) are
    informational and ignored."""
    if text.startswith("No data found in range") or text.startswith("No displayed values found"):
        return {"values": []}

    header = _SHEET_HEADER_RE.match(text)
    if header is None:
        raise MCPClientError(f"Unexpected sheet read response format: {_snippet(text)!r}")
    count = int(header.group(1))

    lines = text.split("\n")[1:]
    if len(lines) < count:
        raise MCPClientError(
            f"Sheet read response reports {count} rows but has only {len(lines)} "
            f"lines: {_snippet(text)!r}"
        )

    values = []
    for index in range(count):
        row_match = _SHEET_ROW_RE.match(lines[index])
        if row_match is None or int(row_match.group(1)) != index + 1:
            raise MCPClientError(
                f"Unparseable sheet row line {lines[index]!r} (expected row "
                f"{index + 1}): {_snippet(text)!r}"
            )
        try:
            row = ast.literal_eval(row_match.group(2))
        except (ValueError, SyntaxError) as e:
            raise MCPClientError(f"Could not parse sheet row line {lines[index]!r}: {e}") from e
        if not isinstance(row, list):
            raise MCPClientError(f"Sheet row line did not parse to a list: {lines[index]!r}")
        while row and row[-1] == "":
            row.pop()
        values.append(row)
    return {"values": values}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _server_url() -> str:
    # Read at call time, not import time, so the URL can be set or changed
    # without reimporting (same convention as the USE_MCP_* kill switches).
    url = os.environ.get(_URL_ENV_VAR, "").strip()
    if not url:
        raise MCPClientError(
            f"{_URL_ENV_VAR} is not set but an MCP-backed tool was called "
            f"(USE_MCP_GMAIL/USE_MCP_SHEETS default to on). Set {_URL_ENV_VAR} to "
            f"the deployed MCP server's URL, or set the kill switch to 0 for the "
            f"raw API path."
        )
    return url.rstrip("/")


def _endpoint_url() -> str:
    url = _server_url()
    return url if url.endswith("/mcp") else url + "/mcp"


def _audience() -> str:
    parts = urlsplit(_server_url())
    return f"{parts.scheme}://{parts.netloc}"


def _user_google_email() -> str:
    email = os.environ.get(_EMAIL_ENV_VAR, "").strip()
    if not email:
        raise MCPClientError(
            f"{_EMAIL_ENV_VAR} is not set. Every google_workspace_mcp tool requires "
            f"a user_google_email argument; set {_EMAIL_ENV_VAR} to the Google "
            f"account the server holds credentials for."
        )
    return email


class _GoogleIDTokenAuth(httpx2.Auth):
    """Attaches a Google-signed identity token (audience = the Cloud Run
    service URL) to every request, refreshing it when it expires. Backed by
    Application Default Credentials, so the same code works locally and on
    Cloud Run under the runtime service account."""

    def __init__(self, audience: str) -> None:
        self._audience = audience
        self._lock = threading.Lock()
        self._credentials: Any = None

    def _bearer_token(self) -> str:
        with self._lock:
            try:
                if self._credentials is None:
                    self._credentials = google_id_token.fetch_id_token_credentials(
                        self._audience, request=GoogleAuthRequest()
                    )
                if not self._credentials.valid:
                    self._credentials.refresh(GoogleAuthRequest())
            except GoogleAuthError as e:
                raise MCPClientError(
                    f"Could not obtain a Google identity token for audience "
                    f"'{self._audience}': {e}. Application Default Credentials must "
                    f"be able to mint identity tokens (a service account key via "
                    f"GOOGLE_APPLICATION_CREDENTIALS, impersonation, or the Cloud "
                    f"Run runtime service account)."
                ) from e
            token = self._credentials.token
            if not token:
                raise MCPClientError(
                    f"Google identity token refresh for '{self._audience}' yielded no token"
                )
            return str(token)

    def sync_auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._bearer_token()}"
        yield request

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {self._bearer_token()}"
        yield request


def _endpoint_auth() -> httpx2.Auth | None:
    """Picks request auth from the URL scheme alone: https is a real Cloud
    Run endpoint and needs an identity token; http is a local listener (a
    `gcloud run services proxy` or a test double) that supplies its own
    credentials, where minting a token would be wrong - and hangs under
    plain user-credential ADC, which cannot mint identity tokens."""
    if urlsplit(_server_url()).scheme == "http":
        _logger.info(
            "%s is http; connecting without Google identity-token auth "
            "(a local proxy or test listener supplies its own credentials)",
            _URL_ENV_VAR,
        )
        return None
    audience = _audience()
    _logger.info(
        "%s is https; attaching Google identity-token auth with audience %s",
        _URL_ENV_VAR,
        audience,
    )
    return _GoogleIDTokenAuth(audience)


# ---------------------------------------------------------------------------
# Transport: persistent streamable-http session on a background event loop
# ---------------------------------------------------------------------------


def _call_tool_text(tool_name: str, arguments: dict) -> str:
    """Calls a server tool and returns the raw text block of its result.
    This is the seam tests stub - everything above it is pure parsing."""
    _ensure_session()
    assert _loop is not None
    future = asyncio.run_coroutine_threadsafe(_call(tool_name, arguments), _loop)
    try:
        return future.result()
    except MCPClientError:
        raise
    except Exception as e:
        raise MCPClientError(f"MCP call '{tool_name}' failed: {type(e).__name__}: {e}") from e


async def _call(tool_name: str, arguments: dict) -> str:
    assert _session is not None
    result = await _session.call_tool(tool_name, arguments)
    text = _extract_text(result)
    if result.is_error:
        raise MCPClientError(f"MCP tool '{tool_name}' returned an error: {text}")
    return text


def _extract_text(result: Any) -> str:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            return str(block.text)
    raise MCPClientError(f"MCP tool result had no text content: {result.content!r}")


def _ensure_session() -> None:
    if _session is None:
        with _session_lock:
            if _session is None:
                # Validate configuration before spinning up the loop thread so
                # a missing URL fails with the actionable message, not a
                # wrapped connect error.
                _endpoint_url()
                _start()


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
    global _session, _session_cm, _transport_cm, _http_client
    http_client = None
    transport_cm = None
    session_cm = None
    try:
        endpoint = _endpoint_url()
        http_client = create_mcp_http_client(auth=_endpoint_auth())
        transport_cm = streamable_http_client(endpoint, http_client=http_client)
        read_stream, write_stream = await transport_cm.__aenter__()

        session_cm = ClientSession(read_stream, write_stream)
        session = await session_cm.__aenter__()
        await session.initialize()

        _http_client = http_client
        _transport_cm = transport_cm
        _session_cm = session_cm
        _session = session
    except BaseException as e:
        errors.append(e)
        # Best-effort teardown of whatever partially came up, so a failed
        # connect doesn't leak an httpx client or half-open session.
        for closer in (
            session_cm.__aexit__(None, None, None) if session_cm is not None else None,
            transport_cm.__aexit__(None, None, None) if transport_cm is not None else None,
            http_client.aclose() if http_client is not None else None,
        ):
            if closer is not None:
                try:
                    await closer
                except BaseException:
                    pass
    finally:
        ready.set()


def _shutdown() -> None:
    global _session, _session_cm, _transport_cm, _http_client, _loop, _loop_thread
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
    _transport_cm = None
    _http_client = None
    _loop = None
    _loop_thread = None


async def _teardown() -> None:
    # Exits in the reverse order they were entered; the transport context
    # sends the session-terminating DELETE, so it must close while the loop
    # and http client are still alive.
    if _session_cm is not None:
        await _session_cm.__aexit__(None, None, None)
    if _transport_cm is not None:
        await _transport_cm.__aexit__(None, None, None)
    if _http_client is not None:
        await _http_client.aclose()
