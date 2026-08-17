from __future__ import annotations

import http.client
from pathlib import Path
import shutil

import pytest

from platforms.chatgpt.sentinel_vm import (
    SentinelSDKManager,
    SentinelVMPool,
    _NodeSentinelWorker,
)
from platforms.chatgpt.environment_profile import PROTOCOL_CHROME_VERSION


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _valid_sdk(marker: str) -> str:
    return (
        f"var SentinelSDK={{token:async function(){{return '{{}}';}}}};"
        f"SentinelSDK.token=SentinelSDK.token;/*{marker}"
        + ("x" * 1200)
        + "*/"
    )


def test_sdk_manager_discovers_and_atomically_caches_new_version(tmp_path):
    sdk_code = _valid_sdk("new-sdk")

    class _Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            if url.endswith("frame.html"):
                return _Response(
                    "<script src='https://sentinel.openai.com/"
                    "sentinel/next123/sdk.js'></script>"
                )
            return _Response(sdk_code)

    session = _Session()
    manager = SentinelSDKManager(cache_dir=tmp_path, refresh_seconds=3600)

    sdk = manager.resolve(session)

    assert sdk.version == "next123"
    assert sdk.path == tmp_path / "sdk-next123.js"
    assert sdk.path.read_text(encoding="utf-8") == sdk_code
    assert (tmp_path / "version.txt").read_text(encoding="utf-8") == "next123"
    assert len(session.urls) == 2

    assert manager.resolve(session) == sdk
    assert len(session.urls) == 2


def test_sdk_manager_uses_bundled_sdk_when_refresh_fails(tmp_path):
    class _Session:
        def get(self, *_args, **_kwargs):
            raise OSError("offline")

    manager = SentinelSDKManager(cache_dir=tmp_path, refresh_seconds=0)
    sdk = manager.resolve(_Session())

    assert sdk.path.name == "sdk.js"
    assert sdk.path.parent.name == "sentinel_vm"
    assert sdk.version
    assert manager.last_error == "offline"


def test_v8_pool_has_a_fixed_bounded_worker_count():
    pool = SentinelVMPool(worker_count=3)
    try:
        assert pool.worker_count == 3
    finally:
        pool.close()


def test_v8_fallback_user_agent_matches_protocol_chrome_version():
    runner = (
        Path(__file__).resolve().parents[1]
        / "platforms"
        / "chatgpt"
        / "sentinel_vm"
        / "sentinel-runner.js"
    ).read_text(encoding="utf-8")

    assert f"Chrome/{PROTOCOL_CHROME_VERSION}.0.0.0" in runner


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_node_v8_server_starts_without_a_browser_process():
    worker = _NodeSentinelWorker(timeout=10, timezone="America/New_York")
    try:
        worker._ensure_started()
        assert worker._process is not None
        assert Path(worker._process.args[1]).name == "sentinel-server.js"

        connection = http.client.HTTPConnection("127.0.0.1", worker._port, timeout=5)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            assert '"ok":true' in response.read().decode("utf-8")
        finally:
            connection.close()
    finally:
        worker.close()
