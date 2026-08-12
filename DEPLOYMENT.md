# Deployment: portability blockers and how they're handled

This project was written to run as a single long-lived local process
(`python main.py`, one person, one machine, one interactive session). This
document covers the four things that assumption breaks when the agent runs
somewhere else — a container, a service with multiple replicas, an
environment with no browser and no human — and what this codebase now does
about each one.

**Scope note:** everything below is a code-level change only. No cloud
resources were created, no OAuth consent flow was run, and no deploy
happened while writing this. Actually running this in Cloud Run (or
anywhere else) still requires real infrastructure work — a GCP project, an
OAuth client, IAM bindings, a container build/push, a live end-to-end
consent round-trip — none of which this document or the commits it
describes performed.

---

## 1. Session state (staged writes)

**The blocker.** `stage_write` computed a diff and persisted it to
`pending_write.json` on local disk; `commit_write` read that file back and
deleted it after applying. Containers are interchangeable and ephemeral —
the container that serves the confirm turn is not guaranteed to be the one
that wrote the file, or to share a disk with it at all.

**How it's handled.** Both tools now take an ADK-injected
`tool_context: ToolContext` parameter and read/write the identical payload
under `tool_context.state["pending_write"]` instead of a file path. This
parameter is excluded from the JSON schema the model sees (verified against
the installed ADK package source — see the "Move pending write from file to
ADK session state" commit message and `ENGINEERING_LOG.md` entry 29 for the
file:line evidence), so it adds no new model-controllable input to a write
tool.

**What this changes operationally.** Session state is scoped to the ADK
session, not the process. `main.py` creates one session per run and reuses
it across every turn of that run's loop, so staging and confirming still
work as two turns of the same running process — but a staged write no
longer survives a process restart (the old file did, for up to the 1-hour
staleness window). This is an accepted trade, not a bug — see
`ENGINEERING_LOG.md` entry 29. It does mean a deployed version needs
sticky routing (or a single instance) between the staging turn and the
confirm turn of the *same conversation*, since a fresh session has no
memory of a prior one's staged write regardless of restart. That's a
routing/session-affinity concern for whatever serves this agent remotely
(e.g. keeping one Cloud Run instance and one ADK session per conversation)
— out of scope for this pass, which only removed the filesystem dependency.

---

## 2. Headless auth

**The blocker.** `auth.py`'s `_build_service` fell through to
`flow.run_local_server(port=0)` — a call that opens a local browser and
blocks until a human completes an OAuth consent screen — whenever no valid
cached token existed. A container has no browser and no human attached to
it, so this doesn't fail; it hangs forever with no error. This was hit for
real, locally, as an `invalid_grant` mid-eval.

**How it's handled.** `HEADLESS=1` (checked at call time inside
`_build_service`, so setting it doesn't require reimporting anything):

- Never calls `run_local_server`, under any circumstance.
- Loads the cached token from the first available source: the
  `GOOGLE_TOKEN_JSON` env var if set and non-empty (the credential's
  authorized-user **JSON as a string** — this is how Agent Runtime delivers
  secrets, since it offers no file mount), else the JSON token file at
  `GOOGLE_TOKEN_PATH` if set, else the local `token.json` default. A legacy
  `token.pickle` found beside a missing JSON token file is migrated to JSON
  once (the pickle is left in place); the pickle path is unreachable when
  `GOOGLE_TOKEN_JSON` is set, so injected env content is never unpickled.
- When the source is `GOOGLE_TOKEN_JSON`, the credential is **read-only**: a
  silent refresh still happens in memory, but the refreshed token is not
  persisted anywhere (logged at INFO). File sources persist refreshes
  atomically, exactly as before.
- Attempts a silent refresh if a refresh token is present
  (`creds.refresh(Request())` — an HTTP call, no browser, no interactivity).
- Raises `RuntimeError` immediately, with a message naming the fix, if
  there's no usable token at all, or if the silent refresh itself fails.

`HEADLESS` unset or `0` is byte-for-byte the previous behavior.

**What this changes operationally.** This makes the *failure mode* safe
(loud and immediate instead of an infinite hang) and makes the *token
source* injectable two ways: a file path via `GOOGLE_TOKEN_PATH`, or the
credential JSON itself via `GOOGLE_TOKEN_JSON` — the latter being the only
mechanism Agent Runtime supports (secrets arrive as env-var strings; there
is no writable filesystem or file mount). It does **not** solve token
provisioning by itself: a deployed instance still needs a valid,
already-authorized token minted locally first and wired into one of those
two sources (e.g. a Secret Manager secret mapped into `GOOGLE_TOKEN_JSON`
via the runtime's `env_vars` secret reference) before it starts serving
traffic. That wiring is deployment configuration, deliberately left undone
here (no Secret Manager or GCS client was added in this pass, per the
scope of this change).

---

## 3. Vertex instead of an API key

**The blocker.** `classify_email.py`, `draft_reply.py`, and
`find_application_date.py` each built their own
`genai.Client(api_key=os.environ["GOOGLE_API_KEY"])`, and `main.py` gated
startup on `GOOGLE_API_KEY` alone. There was no path to use Vertex AI
(project/location + Application Default Credentials) instead of a
developer API key, which matters once this runs as a GCP-hosted service
where ADC is the natural credential source.

**How it's handled.** All three call sites now go through one factory,
`tools/genai_client.get_genai_client()`:

- `GOOGLE_GENAI_USE_ENTERPRISE` true/1 (matched case-insensitively, identical
  to the installed `google-genai` SDK's own parsing) → Vertex-mode client
  built from `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, no
  `api_key`. This is the **preferred** flag.
- `GOOGLE_GENAI_USE_VERTEXAI` is a **compatibility alias** for the same
  thing, honored the same way. If both are set to conflicting values,
  `GOOGLE_GENAI_USE_ENTERPRISE` wins and a warning is emitted — this
  mirrors the installed `google-genai` SDK's own precedence between the two
  env vars exactly, so this factory and the underlying SDK never disagree
  about which mode is selected.
- Otherwise → the original API-key client, unchanged.

`main.py`'s startup check now calls `has_valid_genai_config()`, which
accepts either a complete Vertex config or an API key, and names both
options in its error message if neither is present.

**What this changes operationally.** Model names and generation config are
untouched — this only changes how the client authenticates. A Vertex-mode
deployment still needs the Vertex AI API enabled on the target GCP project
and a service identity with the right IAM role to call it (typically
`roles/aiplatform.user`) — infrastructure setup, not something this code
change can do on its own.

---

## 4. MCP transport coupling (documented, not resolved this pass)

**The blocker.** The optional MCP-backed fetch/read path
(`USE_MCP_GMAIL`, `USE_MCP_SHEETS` — see `MCP_INTEGRATION.md`) talks to a
local [`gemini-cli-extensions/workspace`](https://github.com/gemini-cli-extensions/workspace)
server over **stdio**, which this project's `tools/mcp_client.py` spawns
as a child process: `node <WORKSPACE_MCP_DIR>/workspace-server/dist/index.js`.
That server has two properties that don't survive a deployed, containerized
environment:

- It requires **Node.js >= 20 on `PATH`** and the server's own source
  checked out and built on disk (`WORKSPACE_MCP_DIR`, default
  `~/.workspace-mcp`) — an additional runtime and an additional repo the
  deploy image would need to bundle, on top of this project's own Python
  dependencies.
- Its own OAuth token is stored in the **macOS Keychain**
  (`gemini-cli-workspace-oauth` service). Keychain does not exist on a
  Linux container at all — this is not a "reauthorize" problem, it's a
  storage backend that has no equivalent on the target platform. (A
  separate, prior investigation looked at what it would take to run an MCP
  Workspace server remotely — see `~/scratch/mcp-probe/REMOTE_FINDINGS.md`
  and `~/scratch/mcp-probe/SECURITY_REVIEW.md` if present locally — and
  concluded `gemini-cli-extensions/workspace` specifically would need a
  source patch to its token-storage layer to work at all outside a single
  machine, whereas a different third-party server,
  `taylorwilsdon/google_workspace_mcp`, has an HTTP transport and a
  pluggable credential store built for exactly this. Adopting that
  alternative is a real option but is out of scope here — this pass
  touches only this project's own code, not which MCP server it talks to.)

**How it's handled, for now.** Not fixed — worked around. Both MCP paths
are already kill-switched, defaulting on:

```
USE_MCP_GMAIL=0     # get_email_detail's fetch reverts to the raw Gmail API
USE_MCP_SHEETS=0    # write_to_sheet's sheet read reverts to the raw Sheets API
```

A deployed instance should set both to `0`. The raw-API path is exactly
what `auth.py`'s headless mode (blocker #2) and the Vertex client factory
(blocker #3) harden — the MCP path was never touched by either fix, and
isn't portable as shipped. Setting both flags to `0` is the only
deployable configuration until (and unless) the MCP integration itself is
migrated to a remote-capable server, which is future work, not part of
this pass.

---

## Environment variable matrix

| Variable | Local (current default) | Deployed | Notes |
|---|---|---|---|
| `HEADLESS` | unset (`0`) | `1` | Blocker #2. Never opens a browser when `1`; fails loudly instead. |
| `GOOGLE_TOKEN_PATH` | unset → `token.json` in repo root | path to a pre-authorized token **JSON** file (mounted secret/volume) | Blocker #2. Must exist and be valid *before* the process starts when `HEADLESS=1` — there is no way to mint it interactively in that mode. Ignored when `GOOGLE_TOKEN_JSON` is set. A legacy `token.pickle` beside a missing JSON file is auto-migrated once. |
| `GOOGLE_TOKEN_JSON` | unset | the pre-authorized token's **JSON contents as a string** (e.g. an Agent Runtime `env_vars` secret reference) | Blocker #2. Wins over `GOOGLE_TOKEN_PATH` when set and non-empty. Read-only: refreshed tokens are not persisted (logged at INFO). Deliberately not named `GOOGLE_CLOUD_*` — Agent Runtime reserves that prefix and several exact `GOOGLE_*` names. |
| `GOOGLE_API_KEY` | set (Gemini Developer API) | **must be unset in Vertex mode** | Blocker #3. Required unless in Vertex mode. If set alongside an explicit Vertex-mode `api_key` path and env project/location, the SDK silently discards `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION` and falls back to Vertex express mode — see `_api_client.py:702-709` in the installed `google-genai` package. |
| `GOOGLE_GENAI_USE_ENTERPRISE` | unset | `true` (if using Vertex) | Blocker #3. **Preferred** flag; switches all three Gemini call sites to Vertex mode. Wins on conflict with `GOOGLE_GENAI_USE_VERTEXAI`. |
| `GOOGLE_GENAI_USE_VERTEXAI` | unset | `true` (if using Vertex, older alias) | Blocker #3. Compatibility alias for `GOOGLE_GENAI_USE_ENTERPRISE`; same effect when set alone. |
| `GOOGLE_CLOUD_PROJECT` | unset | required in Vertex mode | Blocker #3. Vertex AI API must be enabled on this project. |
| `GOOGLE_CLOUD_LOCATION` | unset | required in Vertex mode | Blocker #3, e.g. `us-central1`. |
| `USE_MCP_GMAIL` | unset (`1`, MCP on) | `0` | Blocker #4. MCP path is not container-portable; revert to raw API. |
| `USE_MCP_SHEETS` | unset (`1`, MCP on) | `0` | Blocker #4. Same reason. |
| `WORKSPACE_MCP_DIR` | unset → `~/.workspace-mcp` | irrelevant once `USE_MCP_GMAIL`/`USE_MCP_SHEETS` are both `0` | Only read when the MCP path is active. |

Two things this table does **not** cover, because this pass didn't touch
them: `credentials.json` (this project's own OAuth client secret — still
needed regardless of `HEADLESS`, since it's read by the browser-flow branch
that headless mode skips but a *local* re-auth to produce a token still
needs) and `_SPREADSHEET_ID` (hardcoded in `tools/write_to_sheet.py`,
pre-existing, unrelated to any of the four blockers above).

---

## What "deployable" does and doesn't mean after this pass

Done: the four code-level blockers above no longer require modifying
source to work around — each is a flag or an env var. `pending_write.json`,
the browser-only auth fallback, and the API-key-only Gemini client are no
longer hardcoded assumptions.

Not done, and explicitly out of scope for this pass: actually deploying
anywhere, provisioning a token into `GOOGLE_TOKEN_PATH` or
`GOOGLE_TOKEN_JSON`, enabling the Vertex
AI API, configuring Cloud Run IAM/ingress, or migrating the MCP
integration to a remote-capable server. Those are the next, separate body
of work.
