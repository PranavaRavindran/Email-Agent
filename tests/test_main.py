from unittest.mock import MagicMock, patch

import pytest

import main


class TestStartupGenaiConfigCheck:
    def test_exits_when_neither_config_is_present(self, capsys):
        with (
            patch("main.has_valid_genai_config", return_value=False),
            patch("main.initialize_gmail_service") as mock_init,
            patch("main.asyncio.run") as mock_run,
        ):
            with pytest.raises(SystemExit) as excinfo:
                main.main()

        assert excinfo.value.code == 1
        mock_init.assert_not_called()
        mock_run.assert_not_called()

        message = capsys.readouterr().out
        assert "GOOGLE_API_KEY" in message
        assert "GOOGLE_GENAI_USE_VERTEXAI" in message
        assert "GOOGLE_CLOUD_PROJECT" in message
        assert "GOOGLE_CLOUD_LOCATION" in message

    def test_proceeds_when_api_key_config_is_valid(self):
        with (
            patch("main.has_valid_genai_config", return_value=True),
            patch("main.initialize_gmail_service") as mock_init,
            # A plain MagicMock, not the default AsyncMock: run_agent() is only
            # ever passed to the (also mocked) asyncio.run - never awaited
            # directly - so a real coroutine object here would just leak.
            patch("main.run_agent", new=MagicMock(return_value="coro-sentinel")),
            patch("main.asyncio.run") as mock_run,
        ):
            main.main()

        mock_init.assert_called_once()
        mock_run.assert_called_once()

    def test_proceeds_when_vertex_config_is_valid(self):
        # main.main() only ever asks has_valid_genai_config() a yes/no
        # question - it doesn't care which of the two configurations made it
        # true. This pins that main() doesn't special-case either mode.
        with (
            patch("main.has_valid_genai_config", return_value=True),
            patch("main.initialize_gmail_service") as mock_init,
            # A plain MagicMock, not the default AsyncMock: run_agent() is only
            # ever passed to the (also mocked) asyncio.run - never awaited
            # directly - so a real coroutine object here would just leak.
            patch("main.run_agent", new=MagicMock(return_value="coro-sentinel")),
            patch("main.asyncio.run") as mock_run,
        ):
            main.main()

        mock_init.assert_called_once()
        mock_run.assert_called_once()
