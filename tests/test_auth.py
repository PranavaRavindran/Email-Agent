import json
import os
import pickle
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pytest
from google.oauth2.credentials import Credentials

import auth


@pytest.fixture(autouse=True)
def _no_ambient_google_token_json(monkeypatch):
    """GOOGLE_TOKEN_JSON takes precedence over the token file, so a value
    leaked in from the shell would silently reroute every file-path test
    through the env-var source. Tests of the env source set it explicitly."""
    monkeypatch.delenv("GOOGLE_TOKEN_JSON", raising=False)


def _fake_creds(*, valid=False, expired=False, refresh_token=None):
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    return creds


def _fake_token_payload(**overrides):
    """A structurally valid authorized-user payload with obviously fake
    values. expiry defaults far in the future so the credential is valid."""
    payload = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": "fake-client-secret",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "expiry": "2099-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class _FakeJsonCreds:
    """Stands in for a Credentials object in _save_credentials tests: the
    only interface _save_credentials uses is to_json()."""

    def __init__(self, payload):
        self.payload = payload

    def to_json(self):
        return json.dumps(self.payload)


class TestHeadlessAuth:
    """HEADLESS=1 must never call run_local_server - a container has no
    browser and no human, so that call hangs forever with no error at all."""

    def test_no_token_file_raises_and_never_opens_a_browser(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.json"))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        assert "HEADLESS=1" in str(excinfo.value)
        assert "GOOGLE_TOKEN_PATH" in str(excinfo.value)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_expired_token_with_no_refresh_token_raises_and_never_opens_a_browser(
        self, monkeypatch, tmp_path
    ):
        token_path = tmp_path / "token.json"
        token_path.write_text("placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        stale_creds = _fake_creds(valid=False, expired=True, refresh_token=None)

        with (
            patch("auth._load_token_file", return_value=stale_creds),
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        assert "HEADLESS=1" in str(excinfo.value)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_silent_refresh_failure_raises_actionable_error_and_never_opens_a_browser(
        self, monkeypatch, tmp_path
    ):
        # Reproduces the invalid_grant-mid-eval failure this fix was written
        # for: a refresh_token is present, but the silent refresh itself
        # fails (e.g. the token was revoked).
        token_path = tmp_path / "token.json"
        token_path.write_text("placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")
        expired_creds.refresh.side_effect = Exception("invalid_grant")

        with (
            patch("auth._load_token_file", return_value=expired_creds),
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        assert "HEADLESS=1" in str(excinfo.value)
        assert "invalid_grant" in str(excinfo.value)
        assert excinfo.value.__cause__ is not None
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_valid_credentials_succeed_without_touching_the_browser_flow(
        self, monkeypatch, tmp_path
    ):
        # Uses a REAL on-disk JSON token, not a patched loader, so this also
        # exercises the whole load path under HEADLESS=1.
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps(_fake_token_payload()))
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            service = auth._build_service()

        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_called_once()
        loaded = mock_build.call_args.kwargs["credentials"]
        assert isinstance(loaded, Credentials)
        assert loaded.valid
        assert service is mock_build.return_value

    def test_expired_token_with_refresh_token_refreshes_silently(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text("placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")

        with (
            patch("auth._load_token_file", return_value=expired_creds),
            patch("auth._save_credentials") as mock_save,
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            auth._build_service()

        expired_creds.refresh.assert_called_once()
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_save.assert_called_once()
        mock_build.assert_called_once_with("gmail", "v1", credentials=expired_creds)


class TestCorruptTokenFile:
    """A token file that exists but does not load into usable credentials
    must produce an actionable error naming the path and the remedy - not a
    raw JSONDecodeError three frames deep, and never a fall-through to the
    browser flow."""

    def _assert_actionable(self, excinfo, token_path):
        message = str(excinfo.value)
        assert str(token_path) in message
        assert "delete" in message.lower()
        assert "re-authorize" in message.lower()

    def test_zero_byte_token_raises_actionable_error_not_raw_jsondecodeerror(
        self, monkeypatch, tmp_path
    ):
        token_path = tmp_path / "token.json"
        token_path.write_bytes(b"")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_garbage_bytes_token_raises_actionable_error_without_echoing_content(
        self, monkeypatch, tmp_path
    ):
        token_path = tmp_path / "token.json"
        token_path.write_bytes(b"fake-secret-garbage that is definitely not json")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        assert excinfo.value.__cause__ is not None
        # The payload could be a torn credential; the message must never
        # quote it.
        assert "fake-secret-garbage" not in str(excinfo.value)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_json_of_wrong_type_raises_actionable_error(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps(["not", "a", "credentials", "object"]))
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        assert "list" in str(excinfo.value)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_json_missing_credential_fields_raises_actionable_error(self, monkeypatch, tmp_path):
        # Parses fine, is a dict, but cannot silently refresh: not a
        # credentials payload. Must not surface as a KeyError/ValueError.
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps({"access_token": "fake-not-a-credential"}))
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        assert "missing" in str(excinfo.value)
        assert "fake-not-a-credential" not in str(excinfo.value)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_malformed_expiry_value_is_withheld_from_the_error(self, monkeypatch, tmp_path):
        # google-auth parses expiry with strptime, whose ValueError echoes
        # the offending VALUE. A torn payload could shift secret bytes into
        # that field, so the error must withhold it entirely (message and
        # cause chain both).
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps(_fake_token_payload(expiry="fake-leaky-expiry-value")))
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        assert "fake-leaky-expiry-value" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()

    def test_headless_corrupt_token_raises_and_never_opens_a_browser(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_bytes(b"")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        self._assert_actionable(excinfo, token_path)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_flow_cls.from_client_secrets_file.return_value.run_local_server.assert_not_called()
        mock_build.assert_not_called()


class TestValidJsonTokenFile:
    def test_valid_json_token_file_loads_and_yields_usable_credentials(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps(_fake_token_payload()))
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            auth._build_service()

        mock_flow_cls.from_client_secrets_file.assert_not_called()
        loaded = mock_build.call_args.kwargs["credentials"]
        assert isinstance(loaded, Credentials)
        assert loaded.valid
        # Everything a silent refresh needs survived the round-trip.
        assert loaded.refresh_token == "fake-refresh-token"
        assert loaded.client_id == "fake-client-id.apps.googleusercontent.com"
        assert loaded.client_secret == "fake-client-secret"
        assert loaded.expiry == datetime(2099, 1, 1, 0, 0, 0)


class TestEnvVarTokenSource:
    """GOOGLE_TOKEN_JSON is the Agent Runtime source: secrets arrive only as
    env-var strings there. It must win over the file, never be unpickled,
    and never trigger a write."""

    def test_credential_loads_from_env_var_and_token_file_is_never_read(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", json.dumps(_fake_token_payload()))
        # A file whose contents would fail loudly if the loader touched it.
        token_path = tmp_path / "token.json"
        token_path.write_bytes(b"not json - reading me means the env var lost")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with (
            patch("auth._load_token_file") as mock_load_file,
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            auth._build_service()

        mock_load_file.assert_not_called()
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        loaded = mock_build.call_args.kwargs["credentials"]
        assert isinstance(loaded, Credentials)
        assert loaded.refresh_token == "fake-refresh-token"

    def test_refreshed_env_var_credential_is_never_persisted(self, monkeypatch, tmp_path, caplog):
        # Expired-but-refreshable payload: the refresh fires, and the result
        # has nowhere to be written - no file write may be attempted.
        monkeypatch.setenv(
            "GOOGLE_TOKEN_JSON",
            json.dumps(_fake_token_payload(expiry="2020-01-01T00:00:00Z")),
        )
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))

        with (
            patch.object(auth.Credentials, "refresh") as mock_refresh,
            patch("auth._save_credentials") as mock_save,
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build"),
        ):
            with caplog.at_level("INFO", logger="auth"):
                auth._build_service()

        mock_refresh.assert_called_once()
        mock_save.assert_not_called()
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        assert not (tmp_path / "token.json").exists()
        assert any("not persisted" in r.getMessage() for r in caplog.records)

    def test_env_var_set_means_a_present_pickle_is_never_unpickled(self, monkeypatch, tmp_path):
        # The security property of the migration guard: env-var content and
        # anything near it must never reach pickle.load, because unpickling
        # executes arbitrary code.
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", json.dumps(_fake_token_payload()))
        (tmp_path / "token.pickle").write_bytes(b"attacker-controlled bytes")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))

        with (
            patch("auth.pickle.load") as mock_pickle_load,
            patch("auth.InstalledAppFlow"),
            patch("auth.build"),
        ):
            auth._build_service()

        mock_pickle_load.assert_not_called()
        # And no migration write happened either.
        assert not (tmp_path / "token.json").exists()

    def test_malformed_env_var_raises_actionable_error_without_echoing_content(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("GOOGLE_TOKEN_JSON", "fake-secret-not-json")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        message = str(excinfo.value)
        assert "GOOGLE_TOKEN_JSON" in message
        assert "fake-secret-not-json" not in message
        assert str(len("fake-secret-not-json")) in message  # length, not content
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()


class TestLegacyPickleMigration:
    def _real_pickled_creds(self):
        return Credentials(
            token="fake-access-token",
            refresh_token="fake-refresh-token",
            token_uri="https://oauth2.googleapis.com/token",
            client_id="fake-client-id.apps.googleusercontent.com",
            client_secret="fake-client-secret",
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
            expiry=datetime(2099, 1, 1, 0, 0, 0),
        )

    def test_legacy_pickle_migrates_to_json_and_pickle_is_left_in_place(
        self, monkeypatch, tmp_path, caplog
    ):
        pickle_path = tmp_path / "token.pickle"
        pickle_path.write_bytes(pickle.dumps(self._real_pickled_creds()))
        token_path = tmp_path / "token.json"
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build"),
        ):
            with caplog.at_level("INFO", logger="auth"):
                auth._build_service()

        mock_flow_cls.from_client_secrets_file.assert_not_called()
        # JSON written atomically: real file present, no temp file residue,
        # and the pickle untouched.
        assert sorted(os.listdir(tmp_path)) == ["token.json", "token.pickle"]
        with open(token_path, encoding="utf-8") as f:
            migrated = json.load(f)
        assert migrated["refresh_token"] == "fake-refresh-token"
        assert migrated["client_id"] == "fake-client-id.apps.googleusercontent.com"
        assert pickle_path.read_bytes()  # still there, still non-empty
        assert any("migration" in r.getMessage().lower() for r in caplog.records)

    def test_migrated_json_is_loadable_on_the_next_run(self, monkeypatch, tmp_path):
        pickle_path = tmp_path / "token.pickle"
        pickle_path.write_bytes(pickle.dumps(self._real_pickled_creds()))
        token_path = tmp_path / "token.json"
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        with patch("auth.InstalledAppFlow"), patch("auth.build"):
            auth._build_service()  # run 1: migrates

        with (
            patch("auth.pickle.load") as mock_pickle_load,
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            auth._build_service()  # run 2: reads the JSON it just wrote

        mock_pickle_load.assert_not_called()
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        loaded = mock_build.call_args.kwargs["credentials"]
        assert loaded.valid
        assert loaded.refresh_token == "fake-refresh-token"

    def test_corrupt_legacy_pickle_raises_actionable_error(self, monkeypatch, tmp_path):
        pickle_path = tmp_path / "token.pickle"
        pickle_path.write_bytes(b"")
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))

        with patch("auth.InstalledAppFlow") as mock_flow_cls, patch("auth.build") as mock_build:
            with pytest.raises(RuntimeError) as excinfo:
                auth._build_service()

        message = str(excinfo.value)
        assert str(pickle_path) in message
        assert "delete" in message.lower()
        assert isinstance(excinfo.value.__cause__, EOFError)
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_not_called()


class TestHeadlessUnsetPreservesCurrentBehavior:
    def test_no_token_falls_through_to_the_browser_flow(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.json"))

        new_creds = _fake_creds(valid=True)

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth._save_credentials") as mock_save,
            patch("auth.build") as mock_build,
        ):
            mock_flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = (
                new_creds
            )

            auth._build_service()

        mock_flow_cls.from_client_secrets_file.assert_called_once_with(
            auth._CREDENTIALS_FILE, auth.SCOPES
        )
        mock_flow_cls.from_client_secrets_file.return_value.run_local_server.assert_called_once_with(
            port=0
        )
        mock_save.assert_called_once()
        mock_build.assert_called_once_with("gmail", "v1", credentials=new_creds)

    def test_headless_explicitly_zero_falls_through_to_the_browser_flow(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HEADLESS", "0")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.json"))

        new_creds = _fake_creds(valid=True)

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth._save_credentials"),
            patch("auth.build") as mock_build,
        ):
            mock_flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = (
                new_creds
            )

            auth._build_service()

        mock_flow_cls.from_client_secrets_file.return_value.run_local_server.assert_called_once_with(
            port=0
        )
        mock_build.assert_called_once_with("gmail", "v1", credentials=new_creds)

    def test_expired_token_with_refresh_token_still_refreshes_silently(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HEADLESS", raising=False)
        token_path = tmp_path / "token.json"
        token_path.write_text("placeholder")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")

        with (
            patch("auth._load_token_file", return_value=expired_creds),
            patch("auth._save_credentials"),
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build"),
        ):
            auth._build_service()

        expired_creds.refresh.assert_called_once()
        mock_flow_cls.from_client_secrets_file.assert_not_called()

    def test_refresh_failure_propagates_unchanged_instead_of_being_wrapped(
        self, monkeypatch, tmp_path
    ):
        # HEADLESS unset means a refresh failure is not our new RuntimeError -
        # it's the original exception, exactly as before this change.
        monkeypatch.delenv("HEADLESS", raising=False)
        token_path = tmp_path / "token.json"
        token_path.write_text("placeholder")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")
        expired_creds.refresh.side_effect = ValueError("invalid_grant")

        with (
            patch("auth._load_token_file", return_value=expired_creds),
            patch("auth.InstalledAppFlow"),
            patch("auth.build"),
        ):
            with pytest.raises(ValueError, match="invalid_grant"):
                auth._build_service()

    def test_token_path_env_var_absent_defaults_to_local_token_file(self, monkeypatch):
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.delenv("GOOGLE_TOKEN_PATH", raising=False)

        # _save_credentials must be patched out entirely: this is the one
        # test that resolves to the REAL repo token path, and any real write
        # here clobbers a live credential (it zeroed the actual token file
        # twice on 2026-08-11).
        with patch("auth.os.path.exists", return_value=False) as mock_exists:
            with (
                patch("auth.InstalledAppFlow") as mock_flow_cls,
                patch("auth._save_credentials"),
                patch("auth.build"),
            ):
                mock_flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = _fake_creds(
                    valid=True
                )
                auth._build_service()

        # The default resolves to token.json in the repo root, then (absent)
        # falls back to probing for a legacy token.pickle beside it.
        assert auth._TOKEN_FILE == os.path.join(auth._BASE_DIR, "token.json")
        assert mock_exists.call_args_list == [
            call(auth._TOKEN_FILE),
            call(os.path.join(auth._BASE_DIR, "token.pickle")),
        ]


class TestAtomicTokenWrite:
    """_save_credentials must never let the token file pass through a
    partial or empty state - the writer-side counterpart of the corrupt-
    token READER check above. The old open(path, "w") truncated the
    target before writing, so every refresh exposed a zero-byte window,
    and moving to JSON does not change that."""

    def test_successful_write_produces_loadable_file(self, tmp_path):
        token_path = tmp_path / "token.json"

        auth._save_credentials(_FakeJsonCreds({"refresh_token": "rt-123"}), str(token_path))

        with open(token_path, encoding="utf-8") as f:
            assert json.load(f) == {"refresh_token": "rt-123"}

    def test_no_temp_file_remains_after_successful_write(self, tmp_path):
        token_path = tmp_path / "token.json"

        auth._save_credentials(_FakeJsonCreds({"refresh_token": "rt-123"}), str(token_path))

        assert os.listdir(tmp_path) == ["token.json"]

    def test_failed_write_leaves_original_file_intact(self, tmp_path):
        # The property the atomic write exists to provide: a crash after the
        # new bytes are written but before they are durable must not destroy
        # the existing credential.
        token_path = tmp_path / "token.json"
        original = {"refresh_token": "still-good"}
        token_path.write_text(json.dumps(original))

        with patch("auth.os.fsync", side_effect=OSError("disk full mid-write")):
            with pytest.raises(OSError, match="disk full"):
                auth._save_credentials(_FakeJsonCreds({"refresh_token": "new"}), str(token_path))

        with open(token_path, encoding="utf-8") as f:
            assert json.load(f) == original
        assert os.listdir(tmp_path) == ["token.json"]

    def test_existing_permission_mode_is_preserved(self, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text(json.dumps({"refresh_token": "old"}))
        os.chmod(token_path, 0o640)

        auth._save_credentials(_FakeJsonCreds({"refresh_token": "new"}), str(token_path))

        assert os.stat(token_path).st_mode & 0o777 == 0o640
