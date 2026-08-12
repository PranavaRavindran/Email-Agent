# MCP integration: Google Workspace MCP server as a programmatic backend

This document records what was found before writing any code (the mandatory
Step 0 read), the gate decisions that followed from those findings, and the
resulting architecture. It is the reference for anyone touching
`tools/mcp_client.py`, `tools/get_email_detail.py`'s MCP branch, or
`tools/write_to_sheet.py`'s MCP branch later.

## Architecture (unchanged from the plan)

The MCP server's tools are **not** exposed to the model — `agent_spec.yaml`,
the nine model-facing Python tools, and every eval/trajectory stay exactly as
they were. Instead, two of those Python tools become MCP **clients**:

- `get_email_detail`'s internal fetch (raw `users().messages().get` + MIME
  walk + base64 decode + HTML fallback) can be replaced by a single
  `gmail_get` call to the MCP server, mapped into the exact same return
  shape.
- `write_to_sheet`'s **read** of existing Tracker rows (raw
  `spreadsheets().values().get`) can be replaced by a single
  `sheets_getRange` call, mapped into the exact same `values` list shape.

Everything else — `search_email_ids`, `list_emails`, `search_emails`, and the
entire sheet **write** path (`batchUpdate` / `values().update` in
`_apply`/`commit_write`) — stays on the raw Google API on purpose: the server
exposes no Sheets write tools at all, and narrowing the fetch surface further
than "one email, one sheet range" wasn't asked for.

Both migrations are independently kill-switched via env vars read **at call
time** (`os.environ.get(...)`, not at import time), so tests and runtime
toggling both work without reimporting:

- `USE_MCP_GMAIL` (default `"1"`) — `"0"` reverts `get_email_detail`'s fetch
  to the raw Gmail API path.
- `USE_MCP_SHEETS` (default `"1"`) — `"0"` reverts `write_to_sheet`'s read of
  existing rows to the raw Sheets API path.

The raw-API code paths are kept intact behind these flags in this branch;
deletion is deliberately deferred to a later cleanup once evals confirm
parity.

## Step 0 findings

### Server location and startup

The probed, built clone (`~/scratch/mcp-probe/workspace`, self-contained
after `npm install && npm run build`) was moved to a stable location,
`~/.workspace-mcp`, configurable via env `WORKSPACE_MCP_DIR` (default
`~/.workspace-mcp`). `tools/mcp_client.py` checks
`<dir>/workspace-server/dist/index.js` exists at init and fails with a clear
message (including the `npm install && npm run build` instructions) if not.

The server is spawned **directly**:

```
node <WORKSPACE_MCP_DIR>/workspace-server/dist/index.js
```

**Not** via `scripts/start.js` — that wrapper runs `npm install` on every
launch and execs the real server as a child with `stdio: 'inherit'`, so a
`SIGTERM` to the wrapper orphans the actual server holding the parent's stdio
pipes open (confirmed by reading `scripts/start.js` and documented in the
prior probe's `FINDINGS.md`). Spawning `workspace-server/dist/index.js`
directly sidesteps that: the MCP SDK's `stdio_client` spawns the server as
the direct child, in **its own process group/session**
(`_create_platform_compatible_process` in the installed `mcp` package —
see "Python mcp package" below), and on clean async teardown
(`_stop_server_process` → `_terminate_process_tree`) sends SIGTERM to that
whole process group, escalating to SIGKILL if it doesn't exit. So a normal
`mcp_client.py` shutdown already gets process-group-wide cleanup for free
from the SDK; `mcp_client.py`'s `atexit` hook only has to drive that async
teardown to completion.

No `--use-dot-names` flag is passed: default tool naming
(`workspace-server/src/utils/tool-normalization.ts`) replaces every `.` in a
tool name with `_` (`gmail.get` → `gmail_get`, `sheets.getRange` →
`sheets_getRange`); the source comment confirms this underscore form is
specifically meant for "compatibility with a broader set of applications
that use MCP," i.e. non-Gemini-CLI clients like this one.

Node **>=20** is required on `PATH`; `mcp_client.py` checks
`shutil.which("node")` at init (a hard version check is not attempted — not
worth shelling out to `node --version` for a floor that's been true on every
LTS release in years; if it matters in practice the server will simply fail
to start and the error will surface through `mcp_call`).

### Auth model (unchanged from the prior probe, restated here for completeness)

Tool **calls** trigger the server's own OAuth (Google-hosted client,
browser consent), token stored in macOS Keychain under service
`gemini-cli-workspace-oauth`. This is **entirely separate** from this
project's `credentials.json`/`token.json` (used by `auth.py` for the raw
API paths) — both coexist; nothing in `auth.py` changed. `tools/list`
requires no auth at all, which is what makes `scripts/mcp_verify.py`
runnable without triggering consent.

**Trust caveat, verbatim from the prior probe:** every OAuth token refresh
round-trips Google's hosted `geminicli.com` Cloud Function (the server's
default client has no embedded secret; the Cloud Function holds it and does
the code/refresh exchange server-side). Accepted here for a personal
project. Self-hosting your own GCP project + Cloud Function
(`docs/GCP-RECREATION.md`, `scripts/setup-gcp.sh` in the server repo) is the
production alternative if that dependency is ever unacceptable.

### Scope narrowing (server-side, verified from source)

`WORKSPACE_FEATURE_OVERRIDES` is passed in the **spawned process's env**
(not the shell), exactly:

```
WORKSPACE_FEATURE_OVERRIDES=docs.read:off,docs.write:off,drive.read:off,drive.write:off,calendar.read:off,calendar.write:off,chat.read:off,chat.write:off,gmail.write:off,people.read:off,slides.read:off
```

`workspace-server/src/index.ts` wraps `server.registerTool` and skips
registering any tool not in `enabledTools` (computed once at startup from
`resolveFeatures`) — a disabled tool is never in `tools/list`, not just
hidden from the model. `sheets.write`/`slides.write`/`tasks.*` are already
off by default. This leaves `gmail.read` + `sheets.read` (+ scopeless
`time.*`) enabled, narrowing the OAuth consent to `gmail.readonly` +
`spreadsheets.readonly`. `scripts/mcp_verify.py` proves this allowlist
server-side without needing to authenticate.

### Gate G1 — does `gmail_get` accept a Gmail message id? **PASS**

From `tools/list`'s `gmail.get` input schema (`~/scratch/mcp-probe/full_output.txt`):

```json
{
  "name": "gmail.get",
  "description": "Get the full content of a specific email message.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "messageId": { "type": "string", "description": "The ID of the message to retrieve." },
      "format": { "type": "string", "enum": ["minimal", "full", "raw", "metadata"], "description": "Format of the message (default: full)." }
    },
    "required": ["messageId"]
  }
}
```

`workspace-server/src/services/GmailService.ts`'s `get` handler passes
`messageId` straight through as `gmail.users.messages.get({ userId: 'me',
id: messageId, format })` — the identical Gmail message id that
`search_email_ids` (via `users.messages.list`) already returns. No
thread-based mode, no alternate id scheme. Gate passes; `USE_MCP_GMAIL`
defaults to `"1"`.

### Gate G2 — is a faithful response recoverable from `gmail_get`? **PASS**

Read `GmailService.ts`'s `get` handler directly (schema alone doesn't
specify output shape). For `format: 'full'` (the default, and what
`mcp_call("gmail_get", {"messageId": ...})` will use), the handler returns
one `text` content block whose `text` is `JSON.stringify(...)` of:

```json
{
  "id": "...", "threadId": "...", "labelIds": [...], "snippet": "...",
  "subject": "...", "from": "...", "to": "...", "date": "...",
  "body": "...", "attachments": [...]
}
```

`subject`/`from`/`to`/`date` come from a `headers.find(h => h.name === X)`
lookup — same header-extraction approach as `get_email_detail._fetch_one`'s
current code. `body` comes from `extractAttachmentsAndBody`, which
recursively walks `message.payload.parts`, **prefers `text/plain`, falls
back to nothing else** (no HTML-to-text conversion — unlike our own
`_extract_body`, which falls back to HTML converted to text when no
`text/plain` part exists), base64-decodes with `Buffer.from(data,
'base64').toString('utf-8')`, and if the resulting body is falsy, the
handler itself substitutes `message.snippet` (`body: body || message.snippet`).

This last point matters for the mapping: **the server's own fallback means
the wrapper can't distinguish "no plain-text part" from "no body at all"
purely from the response's `body` field** the way the raw path can (the raw
path falls back to HTML, and only truly empty-body messages produce `""`).
Per the migration instructions, the wrapper does not invent new behavior for
this — it takes the MCP response's `body` field as-is (empty string if
absent/empty) and applies the exact same "if not body: print WARNING, else
truncate" logic the raw path already uses. In practice this means a
plain-text-free HTML email will read as its Gmail snippet under
`USE_MCP_GMAIL=1` rather than as HTML-converted-to-text; this is a known,
narrow behavioral difference from the raw path, not a bug, and is exactly
the kind of parity question the kill switch exists to let evals surface.

Gate passes; `USE_MCP_GMAIL` defaults to `"1"`.

**Correction (2026-08-11): the snippet-fallback prediction above is
falsified.** The 2026-08-11 drafting eval failed under `USE_MCP_GMAIL=1`
(final_response_match_v2 0.0) and passed under `USE_MCP_GMAIL=0` with all
metrics 1.0 — same case, same code, only the kill switch differed, which is
exactly the A/B the kill switch exists to provide. What actually came back
from `gmail_get` for a plain-text-free HTML email was **the raw `text/html`
markup itself**, starting `<!doctype html><html lang=en ...<style
type="text/css">...` — not `message.snippet`. Because the extracted body was
truthy, the handler's `body || message.snippet` fallback never fired, so the
"will read as its Gmail snippet" prediction (and the "not a bug" conclusion
that rested on it) does not hold. Since the wrapper truncates at 2000 chars,
the model received only doctype/CSS preamble and the prose was discarded.
The fix: `get_email_detail._fetch_one` now runs `_normalize_body` on the
fetched body (any transport) before truncation, applying the existing
`_html_to_text` when the body is an HTML document. The original reasoning is
kept above, uncorrected, as the record of what was believed at gate time.

### Gate G3 — does `sheets_getRange` faithfully reproduce the current sheet read? **PASS, trivially**

`tools/write_to_sheet.py`'s `_resolve` currently does exactly:

```python
sheets_service.spreadsheets().values().get(
    spreadsheetId=_SPREADSHEET_ID, range=_READ_RANGE
).execute().get("values", [])
```

— a raw 2D list of row values, nothing more. `sheets.getRange`'s handler
(`workspace-server/src/services/SheetsService.ts`) does:

```ts
const response = await sheets.spreadsheets.values.get({ spreadsheetId: id, range });
return { content: [{ type: 'text', text: JSON.stringify({ range: response.data.range, values: response.data.values || [] }) }] };
```

This is the **same underlying Sheets API call**, wrapped in JSON text with
`values` under the same key and the same shape (`response.data.values ||
[]`). The mapping is `json.loads(text)["values"]`, no reinterpretation
needed. `sheets.getText` (rendered text/CSV/JSON of the whole sheet) was
also read and rejected for this use — it would require re-deriving row/column
structure from rendered output instead of getting it directly.

Gate passes; `USE_MCP_SHEETS` defaults to `"1"`.

### Python `mcp` package

**Correction to the task's stated premise:** the `mcp` package was **not**
actually installed in `venv/` — `pip show mcp` failed, and `google-adk`
2.4.0's declared dependencies (`pip show google-adk`) do not include `mcp`.
It was installed fresh (`pip install mcp`, resolved to `mcp==2.0.0` /
`mcp-types==2.0.0`) and pinned into `requirements.txt` alongside its new
transitive deps (`httpx2`, `httpcore2`, `PyJWT`, `sse-starlette`,
`truststore`).

Read from source (`venv/lib/python3.11/site-packages/mcp/client/`):

- `mcp.client.stdio.StdioServerParameters(command, args, env=None, cwd=None,
  encoding="utf-8", encoding_error_handler="strict")` and
  `mcp.client.stdio.stdio_client(server, errlog=sys.stderr)` — an
  `@asynccontextmanager` that spawns the process and yields `(read_stream,
  write_stream)`. `env` is merged **over** `get_default_environment()` (a
  fixed allowlist of inherited vars), not appended to the full parent
  environment — `mcp_client.py` passes `os.environ | {WORKSPACE_FEATURE_OVERRIDES: ...}`
  explicitly as `env` rather than relying on inheritance, so nothing needed
  by the server (e.g. `PATH`, `HOME`) is silently dropped.
- `mcp.client.session.ClientSession(read_stream, write_stream, ...)` — async
  context manager; `await session.initialize()`; `await
  session.call_tool(name, arguments)` returns a `CallToolResult` with
  `.content` (list of content blocks, typically one `TextContent` with
  `.text` holding the tool's JSON string) and `.is_error`.
- **Concurrency: `ClientSession` supports concurrent in-flight requests.**
  Its `JSONRPCDispatcher` (`mcp/shared/jsonrpc_dispatcher.py`) tracks
  requests in `self._in_flight: dict[RequestId, _InFlight]`, keyed by
  JSON-RPC request id — i.e. genuine id multiplexing, not one-at-a-time. This
  means `mcp_client.py` does **not** need to serialize `mcp_call`s with a
  lock for correctness; multiple coroutines scheduled on the background
  event loop (e.g. from `get_emails_bulk`'s 8 pool threads, each bridging in
  via `run_coroutine_threadsafe`) can have calls genuinely in flight at once
  against the one server process. No serialization performance note is
  needed as a result — the earlier assumption that ~50-email bulk fetches
  might need serialization doesn't apply to this SDK version.

## Allowlist verification (`scripts/mcp_verify.py`)

Run without authentication (`tools/list` needs none). Actual output on this
machine, confirming `WORKSPACE_FEATURE_OVERRIDES` narrows the 57-tool
default surface down to gmail.read + sheets.read + the always-on `time.*`
and `auth.*` tools:

```
[mcp_verify] 12 tools survived the allowlist:
  auth_clear
  auth_refreshToken
  gmail_downloadAttachment
  gmail_get
  gmail_listLabels
  gmail_search
  sheets_getMetadata
  sheets_getRange
  sheets_getText
  time_getCurrentDate
  time_getCurrentTime
  time_getTimeZone
[mcp_verify] PASSED: allowlist took effect server-side.
```

Note `gmail_listLabels` survives - it's part of `gmail.read`, not
`gmail.write`, contrary to what FINDINGS.md's scope table's parenthetical
("`gmail.modify` covers send/createDraft/sendDraft/labels/modify/
batchModify") suggested before actually listing the tools. Harmless either
way (it's a read-only tool - listing label names, not applying them), but
recorded here since it's a case where running the check beat trusting the
inference from source.

## Kill-switch summary

| Flag | Default | `"0"` reverts to |
|---|---|---|
| `USE_MCP_GMAIL` | `"1"` | raw `users().messages().get` + MIME walk in `get_email_detail._fetch_one` |
| `USE_MCP_SHEETS` | `"1"` | raw `spreadsheets().values().get` in `write_to_sheet._resolve` |

Both are read from `os.environ` at call time inside the respective
functions, not cached at import time.
