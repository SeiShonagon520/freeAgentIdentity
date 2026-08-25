"""Runtime capability checks shared by API and task execution."""
from __future__ import annotations

import os


SERVER_RUNTIME_VALUES = {"server", "service", "docker", "headless"}


def runtime_mode() -> str:
    value = str(os.getenv("APP_RUNTIME_MODE", "desktop") or "desktop").strip().lower()
    return value or "desktop"


def is_server_runtime() -> bool:
    return runtime_mode() in SERVER_RUNTIME_VALUES


def har_capture_available() -> bool:
    # HAR may contain cookies, one-time codes and complete request payloads;
    # the public release does not expose capture capability.
    return False
