import json
import logging
import os
import pickle
import tempfile
import threading
from collections.abc import Callable
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# googleapiclient.discovery.build() returns a dynamically generated Resource
# type that untyped stubs make impractical to annotate precisely.
GmailService = Any

_logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CREDENTIALS_FILE = os.path.join(_BASE_DIR, "credentials.json")
_TOKEN_FILE = os.path.join(_BASE_DIR, "token.json")

# Pre-JSON versions persisted the token with pickle under this name. It is
# only used to FIND a legacy file for one-time migration; nothing writes it.
_LEGACY_TOKEN_BASENAME = "token.pickle"

_TOKEN_PATH_ENV_VAR = "GOOGLE_TOKEN_PATH"
_TOKEN_JSON_ENV_VAR = "GOOGLE_TOKEN_JSON"

_HEADLESS_AUTH_ERROR = (
    "HEADLESS=1 but no valid Google credentials are available at '{token_path}' "
    "({reason}). Headless mode never opens a browser or waits for a human, so it "
    "fails immediately instead of hanging. Fix: reauthorize locally with HEADLESS "
    "unset or 0 (this runs the interactive browser flow once and rewrites the "
    "token file), then make the resulting token available to this environment, "
    "either at the path " + _TOKEN_PATH_ENV_VAR + " points to or as the value of "
    "the " + _TOKEN_JSON_ENV_VAR + " environment variable."
)

_CORRUPT_TOKEN_ERROR = (
    "Token file '{token_path}' exists but did not yield usable credentials "
    "({reason}). The file is likely empty, truncated, or not an authorized-user "
    "JSON credential. Fix: delete it and re-authorize (run once with "
    "HEADLESS unset or 0 to go through the interactive browser flow, which "
    "rewrites the token file)."
)

_CORRUPT_ENV_TOKEN_ERROR = (
    _TOKEN_JSON_ENV_VAR + " is set but did not yield usable credentials "
    "({reason}). The payload was {length} bytes; its content is deliberately "
    "never shown because it may contain a partial secret. Fix: re-authorize "
    "locally with HEADLESS unset or 0, then set " + _TOKEN_JSON_ENV_VAR + " to "
    "the exact contents of the token JSON file that flow writes."
)

_service: GmailService | None = None
_service_lock = threading.Lock()
_thread_local = threading.local()


def get_gmail_service() -> GmailService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = _build_service()
    return _service


def get_thread_local_gmail_service() -> GmailService:
    """Returns a Gmail service instance private to the calling thread.

    googleapiclient's build() wires up httplib2 as the default transport,
    and httplib2.Http instances are explicitly not thread-safe - concurrent
    .execute() calls sharing one service object corrupt each other's
    requests. Any code that calls the Gmail API from multiple threads (e.g.
    get_emails_bulk's worker pool) must use this instead of
    get_gmail_service(), so each thread gets its own httplib2.Http under
    the hood. The underlying credentials object is still shared and reused
    across threads, so this does not repeat the OAuth/token-refresh flow.
    """
    if not hasattr(_thread_local, "service"):
        creds = get_gmail_service()._http.credentials
        _thread_local.service = build("gmail", "v1", credentials=creds)
    return _thread_local.service


def initialize_gmail_service() -> GmailService:
    """Run OAuth flow eagerly and cache the service. Call this at startup."""
    global _service
    _service = _build_service()
    return _service


def _credentials_from_json_payload(payload: str, error_for: Callable[[str], str]) -> Credentials:
    """Parse an authorized-user JSON payload into Credentials, strictly.

    Every failure mode - unparseable JSON, JSON of the wrong shape, JSON
    missing the fields a silent refresh needs - raises RuntimeError built
    from error_for(reason), so callers never see a bare JSONDecodeError or
    ValueError. No reason string ever contains payload content: json errors
    name positions, and google-auth's missing-fields error names only field
    names. The one google-auth failure that DOES echo a field value (strptime
    on a malformed expiry) is replaced with a generic reason and raised from
    None so the value cannot leak through the cause chain either.
    """
    try:
        info = json.loads(payload)
    except ValueError as e:  # json.JSONDecodeError subclasses ValueError
        raise RuntimeError(error_for(f"not valid JSON: {e}")) from e
    if not isinstance(info, dict):
        raise RuntimeError(
            error_for(f"JSON parsed to {type(info).__name__}, not a credentials object")
        )
    try:
        return Credentials.from_authorized_user_info(info)
    except ValueError as e:
        if "was not in the expected format" in str(e):
            raise RuntimeError(error_for(f"JSON is not a credentials payload: {e}")) from e
        raise RuntimeError(
            error_for(
                "JSON is not a credentials payload: a credential field is "
                "present but malformed (value withheld)"
            )
        ) from None


def _load_token_file(token_path: str) -> Credentials:
    # Existence is not validity: a zero-byte, truncated, or non-JSON token
    # file passes the caller's path check and would otherwise crash with an
    # error naming neither the file nor the remedy. Failures here raise the
    # actionable error in BOTH modes - never fall through to the browser
    # flow, and in particular HEADLESS=1 must still raise immediately.
    with open(token_path, "rb") as f:
        raw = f.read()
    try:
        payload = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(
            _CORRUPT_TOKEN_ERROR.format(
                token_path=token_path,
                reason=(
                    "not UTF-8 text; if this is a legacy pickled token, point "
                    + _TOKEN_PATH_ENV_VAR
                    + " at a .json path instead so the one-time migration can run"
                ),
            )
        ) from e
    return _credentials_from_json_payload(
        payload,
        lambda reason: _CORRUPT_TOKEN_ERROR.format(token_path=token_path, reason=reason),
    )


def _load_legacy_pickle_for_migration(pickle_path: str) -> Any:
    """Read a legacy pickled credential, strictly, for one-time migration only.

    This is the sole remaining pickle.load in the codebase: unpickling
    executes arbitrary code, which is one of the two reasons the on-disk
    format moved to JSON. The caller guarantees this is unreachable when
    GOOGLE_TOKEN_JSON is set - an injected env payload is never a pickle,
    and must never be fed to pickle.load.
    AttributeError covers pickles whose class can no longer be imported.
    """
    try:
        with open(pickle_path, "rb") as f:
            creds = pickle.load(f)
    except (EOFError, pickle.UnpicklingError, AttributeError) as e:
        raise RuntimeError(
            _CORRUPT_TOKEN_ERROR.format(token_path=pickle_path, reason=f"{type(e).__name__}: {e}")
        ) from e
    if not hasattr(creds, "valid") or not hasattr(creds, "to_json"):
        raise RuntimeError(
            _CORRUPT_TOKEN_ERROR.format(
                token_path=pickle_path,
                reason=(
                    f"unpickled object of type {type(creds).__name__} is not a credentials object"
                ),
            )
        )
    return creds


def _save_credentials(creds: Any, token_path: str) -> None:
    """Persist credentials as authorized-user JSON atomically so no reader
    ever sees a partial file.

    A plain open(token_path, "w") truncates the target before writing, so
    every refresh passes the file through a zero-byte state; any interruption
    in that window destroys the refresh token and forces interactive re-auth.
    Serializing to JSON does not change that. Write to a temp file in the
    same directory (os.replace is only atomic within one filesystem), fsync
    it so the bytes are on disk, then swap it in. The token is a credential:
    an existing target's permission mode is preserved, and a brand-new file
    keeps mkstemp's owner-only 0o600.
    """
    payload = creds.to_json()
    directory = os.path.dirname(token_path) or "."
    try:
        mode = os.stat(token_path).st_mode & 0o777
    except FileNotFoundError:
        mode = None
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, token_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _build_service() -> GmailService:
    # Read at call time, not import time, so tests and deployments can set
    # these without reimporting the module.
    headless = os.environ.get("HEADLESS", "0") == "1"
    token_path = os.environ.get(_TOKEN_PATH_ENV_VAR, _TOKEN_FILE)
    env_payload = os.environ.get(_TOKEN_JSON_ENV_VAR) or ""
    creds = None

    if env_payload:
        # Env-var source (Agent Runtime delivers secrets only as env-var
        # strings). This source is read-only, and by returning here it also
        # guarantees the legacy-pickle migration below is unreachable: env
        # content is never fed to pickle.load.
        _logger.info(
            "Google credential source: %s environment variable (%d bytes); "
            "the token file at '%s' will not be read.",
            _TOKEN_JSON_ENV_VAR,
            len(env_payload.encode("utf-8")),
            token_path,
        )
        creds = _credentials_from_json_payload(
            env_payload,
            lambda reason: _CORRUPT_ENV_TOKEN_ERROR.format(
                reason=reason, length=len(env_payload.encode("utf-8"))
            ),
        )
    elif os.path.exists(token_path):
        _logger.info("Google credential source: token file '%s'.", token_path)
        creds = _load_token_file(token_path)
    else:
        legacy_path = os.path.join(os.path.dirname(token_path) or ".", _LEGACY_TOKEN_BASENAME)
        if os.path.exists(legacy_path):
            creds = _load_legacy_pickle_for_migration(legacy_path)
            _save_credentials(creds, token_path)
            _logger.info(
                "Google credential source: one-time migration of legacy pickle "
                "'%s' to JSON token file '%s'. The pickle was left in place; "
                "delete it manually once the JSON path is confirmed working.",
                legacy_path,
                token_path,
            )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                if headless:
                    raise RuntimeError(
                        _HEADLESS_AUTH_ERROR.format(
                            token_path=token_path, reason=f"silent refresh failed: {e}"
                        )
                    ) from e
                raise
        elif headless:
            if creds is None:
                reason = "no token file found and " + _TOKEN_JSON_ENV_VAR + " is unset"
            elif env_payload:
                reason = "credential from " + _TOKEN_JSON_ENV_VAR + " is not refreshable"
            else:
                reason = "token present but not refreshable"
            raise RuntimeError(_HEADLESS_AUTH_ERROR.format(token_path=token_path, reason=reason))
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        if env_payload:
            # A refreshed credential has nowhere to go when the source is an
            # injected env var (Agent Runtime re-reads the mounted value on
            # the next instance, so dropping it there is correct). Log it so
            # a LOCAL run configured this way is not a silent no-persist.
            _logger.info(
                "Refreshed Google credential was not persisted: the credential "
                "source is the read-only %s environment variable, so there is "
                "no token file to rewrite.",
                _TOKEN_JSON_ENV_VAR,
            )
        else:
            _save_credentials(creds, token_path)

    return build("gmail", "v1", credentials=creds)
