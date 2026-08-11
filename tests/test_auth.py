from unittest.mock import MagicMock, patch

import pytest

import auth


def _fake_creds(*, valid=False, expired=False, refresh_token=None):
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    return creds


class TestHeadlessAuth:
    """HEADLESS=1 must never call run_local_server - a container has no
    browser and no human, so that call hangs forever with no error at all."""

    def test_no_token_file_raises_and_never_opens_a_browser(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.pickle"))

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
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        stale_creds = _fake_creds(valid=False, expired=True, refresh_token=None)

        with (
            patch("auth.pickle.load", return_value=stale_creds),
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
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")
        expired_creds.refresh.side_effect = Exception("invalid_grant")

        with (
            patch("auth.pickle.load", return_value=expired_creds),
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
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        valid_creds = _fake_creds(valid=True)

        with (
            patch("auth.pickle.load", return_value=valid_creds),
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            service = auth._build_service()

        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_build.assert_called_once_with("gmail", "v1", credentials=valid_creds)
        assert service is mock_build.return_value

    def test_expired_token_with_refresh_token_refreshes_silently(self, monkeypatch, tmp_path):
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("HEADLESS", "1")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")

        with (
            patch("auth.pickle.load", return_value=expired_creds),
            patch("auth.pickle.dump") as mock_dump,
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.build") as mock_build,
        ):
            auth._build_service()

        expired_creds.refresh.assert_called_once()
        mock_flow_cls.from_client_secrets_file.assert_not_called()
        mock_dump.assert_called_once()
        mock_build.assert_called_once_with("gmail", "v1", credentials=expired_creds)


class TestHeadlessUnsetPreservesCurrentBehavior:
    def test_no_token_falls_through_to_the_browser_flow(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.pickle"))

        new_creds = _fake_creds(valid=True)

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.pickle.dump") as mock_dump,
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
        mock_dump.assert_called_once()
        mock_build.assert_called_once_with("gmail", "v1", credentials=new_creds)

    def test_headless_explicitly_zero_falls_through_to_the_browser_flow(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HEADLESS", "0")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing-token.pickle"))

        new_creds = _fake_creds(valid=True)

        with (
            patch("auth.InstalledAppFlow") as mock_flow_cls,
            patch("auth.pickle.dump"),
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
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")

        with (
            patch("auth.pickle.load", return_value=expired_creds),
            patch("auth.pickle.dump"),
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
        token_path = tmp_path / "token.pickle"
        token_path.write_bytes(b"placeholder")
        monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(token_path))

        expired_creds = _fake_creds(valid=False, expired=True, refresh_token="rt-123")
        expired_creds.refresh.side_effect = ValueError("invalid_grant")

        with (
            patch("auth.pickle.load", return_value=expired_creds),
            patch("auth.InstalledAppFlow"),
            patch("auth.build"),
        ):
            with pytest.raises(ValueError, match="invalid_grant"):
                auth._build_service()

    def test_token_path_env_var_absent_defaults_to_local_token_file(self, monkeypatch):
        monkeypatch.delenv("HEADLESS", raising=False)
        monkeypatch.delenv("GOOGLE_TOKEN_PATH", raising=False)

        with patch("auth.os.path.exists", return_value=False) as mock_exists:
            with (
                patch("auth.InstalledAppFlow") as mock_flow_cls,
                patch("auth.pickle.dump"),
                patch("auth.build"),
            ):
                mock_flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = _fake_creds(
                    valid=True
                )
                auth._build_service()

        mock_exists.assert_called_once_with(auth._TOKEN_FILE)
