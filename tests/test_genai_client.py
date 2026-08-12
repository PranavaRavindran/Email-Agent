import warnings
from unittest.mock import patch

import genai_client
import pytest


class TestGetGenaiClient:
    def test_api_key_client_when_vertexai_flag_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        with patch("genai_client.genai.Client") as mock_client_cls:
            client = genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(api_key="test-api-key")
        assert client is mock_client_cls.return_value

    def test_vertex_client_when_vertexai_flag_true(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        with patch("genai_client.genai.Client") as mock_client_cls:
            client = genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1"
        )
        assert client is mock_client_cls.return_value
        # No api_key must ever reach the Vertex-mode call.
        assert "api_key" not in mock_client_cls.call_args.kwargs

    def test_vertexai_flag_accepts_1(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "1")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        with patch("genai_client.genai.Client") as mock_client_cls:
            genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1"
        )

    def test_vertexai_flag_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        with patch("genai_client.genai.Client") as mock_client_cls:
            genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1"
        )

    def test_vertexai_flag_false_uses_api_key(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

        with patch("genai_client.genai.Client") as mock_client_cls:
            genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(api_key="test-api-key")

    def test_vertex_client_when_enterprise_flag_true(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        with patch("genai_client.genai.Client") as mock_client_cls:
            genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1"
        )
        assert "api_key" not in mock_client_cls.call_args.kwargs

    def test_enterprise_flag_accepts_truthy_values(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        for value in ["1", "TRUE", "True"]:
            monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", value)

            with patch("genai_client.genai.Client") as mock_client_cls:
                genai_client.get_genai_client()

            mock_client_cls.assert_called_once_with(
                vertexai=True, project="my-project", location="us-central1"
            )
            assert "api_key" not in mock_client_cls.call_args.kwargs

    def test_enterprise_flag_false_uses_api_key(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "false")
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

        with patch("genai_client.genai.Client") as mock_client_cls:
            genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(api_key="test-api-key")

    def test_enterprise_false_vertexai_true_conflict_uses_api_key_and_warns(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "false")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

        with patch("genai_client.genai.Client") as mock_client_cls:
            with pytest.warns(UserWarning, match="GOOGLE_GENAI_USE_ENTERPRISE"):
                genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(api_key="test-api-key")

    def test_enterprise_and_vertexai_agreeing_does_not_warn(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with patch("genai_client.genai.Client") as mock_client_cls:
                genai_client.get_genai_client()

        mock_client_cls.assert_called_once_with(
            vertexai=True, project="my-project", location="us-central1"
        )
        assert "api_key" not in mock_client_cls.call_args.kwargs


class TestHasValidGenaiConfig:
    def test_true_with_api_key_and_vertexai_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")

        assert genai_client.has_valid_genai_config() is True

    def test_false_with_neither_configured(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        assert genai_client.has_valid_genai_config() is False

    def test_true_with_vertexai_flag_and_complete_config(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        assert genai_client.has_valid_genai_config() is True

    def test_false_with_vertexai_flag_but_missing_location(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        assert genai_client.has_valid_genai_config() is False

    def test_false_with_vertexai_flag_and_only_api_key_set(self, monkeypatch):
        # A stray API key must not paper over an incomplete Vertex config -
        # get_genai_client() would still build the (incomplete) Vertex client,
        # never falling back to the API key, so the check must not either.
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)

        assert genai_client.has_valid_genai_config() is False

    def test_true_with_enterprise_flag_and_no_api_key(self, monkeypatch):
        # This is the exact shape of the step 5 eval command: the Vertex
        # A/B exports GOOGLE_GENAI_USE_ENTERPRISE with no GOOGLE_API_KEY set.
        monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        assert genai_client.has_valid_genai_config() is True
