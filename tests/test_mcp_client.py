import threading
import time

import pytest

import tools.mcp_client as mc


def _reset_globals():
    mc._session = None
    mc._session_cm = None
    mc._stdio_cm = None
    mc._loop = None
    mc._loop_thread = None


class TestEnsureSessionInitOnce:
    def setup_method(self):
        _reset_globals()

    def teardown_method(self):
        # Stop any loop/thread a test actually started, without ever
        # spawning or touching a real server process.
        if mc._loop is not None and not mc._loop.is_closed():
            mc._loop.call_soon_threadsafe(mc._loop.stop)
            if mc._loop_thread is not None:
                mc._loop_thread.join(timeout=2)
        _reset_globals()

    def test_concurrent_first_calls_initialize_once(self, monkeypatch):
        connect_calls = []

        async def fake_connect(ready, errors):
            connect_calls.append(1)
            time.sleep(0.05)
            mc._session = object()
            ready.set()

        monkeypatch.setattr(mc, "_connect", fake_connect)

        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            mc._ensure_session()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(connect_calls) == 1
        assert mc._session is not None


class TestStartupErrors:
    def setup_method(self):
        _reset_globals()

    def teardown_method(self):
        _reset_globals()

    def test_clean_error_when_node_missing(self, monkeypatch):
        monkeypatch.setattr(mc.shutil, "which", lambda cmd: None)

        with pytest.raises(mc.MCPClientError, match="node"):
            mc._ensure_session()

        assert mc._session is None
        assert mc._loop is None

    def test_clean_error_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mc.shutil, "which", lambda cmd: "/usr/bin/node")
        monkeypatch.setattr(mc, "_workspace_mcp_dir", lambda: tmp_path / "does-not-exist")

        with pytest.raises(mc.MCPClientError, match="npm install"):
            mc._ensure_session()

        assert mc._session is None
        assert mc._loop is None
