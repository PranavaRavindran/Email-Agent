import pytest


@pytest.fixture(autouse=True)
def _default_to_raw_api_paths(monkeypatch):
    """Existing tests were written against the raw Gmail/Sheets API paths
    and monkeypatch those seams directly. Defaulting both MCP kill switches
    off here keeps every pre-existing test exercising the same path it
    always did, with zero changes to its assertions; tests of the MCP paths
    override the flag explicitly."""
    monkeypatch.setenv("USE_MCP_GMAIL", "0")
    monkeypatch.setenv("USE_MCP_SHEETS", "0")


@pytest.fixture(autouse=True)
def _default_genai_mode_unset(monkeypatch):
    """While genai_client only read GOOGLE_GENAI_USE_VERTEXAI, a test that
    cleared that one name was hermetic. Now that GOOGLE_GENAI_USE_ENTERPRISE
    is also honoured, an exported ENTERPRISE flag - which the Vertex eval
    runs export - would silently flip API-key-path tests into Vertex mode
    if it leaked in from the shell environment. Clear both names here;
    fixtures run before the test body, so tests that set either flag
    explicitly still win.
    """
    monkeypatch.delenv("GOOGLE_GENAI_USE_ENTERPRISE", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
