import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_sentry import TOKEN, fake_sentry  # noqa: E402


def server_command() -> list[str]:
    """The server as a process: the source by default, or a built executable via MCP_SERVER_UNDER_TEST (CI)."""
    built = os.environ.get("MCP_SERVER_UNDER_TEST")
    return [built] if built else [sys.executable, "-m", "sentry_mcp"]


@pytest.fixture
def sentry():
    with fake_sentry() as server:
        yield server


@pytest.fixture
def env(sentry, monkeypatch):
    for name in list(os.environ):
        if name.startswith("SENTRY_"):
            monkeypatch.delenv(name)
    # Never touch the developer's real credential store from the tests.
    values = {"SENTRY_URL": sentry.url, "SENTRY_AUTH_TOKEN": TOKEN, "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture
def client(env):
    from sentry_mcp.client import SentryClient
    from sentry_mcp.config import Settings

    return SentryClient(Settings.from_env())
