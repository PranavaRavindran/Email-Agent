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
