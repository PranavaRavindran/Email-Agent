import os
import pickle
import threading
from typing import Any

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# googleapiclient.discovery.build() returns a dynamically generated Resource
# type that untyped stubs make impractical to annotate precisely.
GmailService = Any

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CREDENTIALS_FILE = os.path.join(_BASE_DIR, "credentials.json")
_TOKEN_FILE = os.path.join(_BASE_DIR, "token.pickle")

_TOKEN_PATH_ENV_VAR = "GOOGLE_TOKEN_PATH"

_HEADLESS_AUTH_ERROR = (
    "HEADLESS=1 but no valid Google credentials are available at '{token_path}' "
    "({reason}). Headless mode never opens a browser or waits for a human, so it "
    "fails immediately instead of hanging. Fix: reauthorize locally with HEADLESS "
    "unset or 0 (this runs the interactive browser flow once and rewrites the "
    "token file), then make the resulting token available to this environment at "
    "the path " + _TOKEN_PATH_ENV_VAR + " points to."
)

_CORRUPT_TOKEN_ERROR = (
    "Token file '{token_path}' exists but did not yield usable credentials "
    "({reason}). The file is likely empty, truncated, or not a pickled "
    "credentials object. Fix: delete it and re-authorize (run once with "
    "HEADLESS unset or 0 to go through the interactive browser flow, which "
    "rewrites the token file)."
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


def _build_service() -> GmailService:
    # Read at call time, not import time, so tests and deployments can set
    # these without reimporting the module.
    headless = os.environ.get("HEADLESS", "0") == "1"
    token_path = os.environ.get(_TOKEN_PATH_ENV_VAR, _TOKEN_FILE)
    creds = None

    if os.path.exists(token_path):
        # Existence is not validity: a zero-byte or corrupt token file passes
        # the path check and would crash inside pickle.load with an error
        # naming neither the file nor the remedy. Failures here raise the
        # actionable error in BOTH modes - never fall through to the browser
        # flow, and in particular HEADLESS=1 must still raise immediately.
        # AttributeError covers pickles whose class can no longer be imported.
        try:
            with open(token_path, "rb") as f:
                creds = pickle.load(f)
        except (EOFError, pickle.UnpicklingError, AttributeError) as e:
            raise RuntimeError(
                _CORRUPT_TOKEN_ERROR.format(
                    token_path=token_path, reason=f"{type(e).__name__}: {e}"
                )
            ) from e
        if not hasattr(creds, "valid"):
            raise RuntimeError(
                _CORRUPT_TOKEN_ERROR.format(
                    token_path=token_path,
                    reason=(
                        f"unpickled object of type {type(creds).__name__} "
                        "is not a credentials object"
                    ),
                )
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
            reason = "no token file found" if creds is None else "token present but not refreshable"
            raise RuntimeError(_HEADLESS_AUTH_ERROR.format(token_path=token_path, reason=reason))
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)
