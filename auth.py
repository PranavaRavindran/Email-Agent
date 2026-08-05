import os
import pickle
import threading

import google_auth_httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CREDENTIALS_FILE = os.path.join(_BASE_DIR, "credentials.json")
_TOKEN_FILE = os.path.join(_BASE_DIR, "token.pickle")

_service = None
_thread_local = threading.local()


def get_gmail_service():
    global _service
    if _service is None:
        _service = _build_service()
    return _service


def get_thread_local_gmail_service():
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


def initialize_gmail_service():
    """Run OAuth flow eagerly and cache the service. Call this at startup."""
    global _service
    _service = _build_service()
    return _service


def _build_service():
    creds = None

    if os.path.exists(_TOKEN_FILE):
        with open(_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)
