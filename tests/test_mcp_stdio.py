"""The real server process over stdio, the way Claude Desktop / Claude Code runs it."""

import json
import os
import socket
import subprocess

import anyio
from conftest import server_command
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_TOOLS = {
    "sentry_info",
    "list_projects",
    "list_teams",
    "find_members",
    "search_issues",
    "get_issue",
    "list_issue_events",
    "get_event",
    "get_issue_tag_values",
    "search_events",
    "list_releases",
}
WRITE_TOOLS = {"update_issues", "add_issue_comment"}


def _session(env: dict, action):
    command = server_command()
    params = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env={**{k: v for k, v in os.environ.items() if not k.startswith("SENTRY_")}, **env},
    )

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await action(session)

    return anyio.run(run)


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def _tools(session):
    return {tool.name: tool for tool in (await session.list_tools()).tools}


def test_full_access_by_default_and_read_only_hides_writes(env):
    tools = _session(env, _tools)
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert tools["search_issues"].annotations.read_only_hint is True
    assert tools["update_issues"].annotations.read_only_hint is False

    tools = _session({**env, "SENTRY_READ_ONLY": "true"}, _tools)
    assert set(tools) == READ_TOOLS


def test_search_over_stdio_returns_structured_json(env):
    async def call(session):
        return await session.call_tool("search_issues", {"limit": 2, "projects": ["api"]})

    result = _session(env, call)
    assert not result.is_error
    payload = result.structured_content or json.loads(_text(result))
    assert [i["short_id"] for i in payload["issues"]] == ["API-1A", "WEB-2"]


def test_resolve_over_stdio(env, sentry):
    async def call(session):
        return await session.call_tool("update_issues", {"issues": ["API-1A"], "status": "resolved_in_next_release"})

    result = _session(env, call)
    assert not result.is_error, _text(result)
    assert sentry.last("PUT", "/issues/4512/")["json"] == {"status": "resolved", "statusDetails": {"inNextRelease": True}}


def test_unknown_issue_comes_back_as_readable_error(env):
    async def call(session):
        return await session.call_tool("get_issue", {"issue": "999999"})

    result = _session(env, call)
    assert result.is_error
    assert "Not found (404" in _text(result)


def test_bad_token_is_explained(env):
    async def call(session):
        return await session.call_tool("sentry_info", {})

    result = _session({**env, "SENTRY_AUTH_TOKEN": "sntryu_wrong"}, call)
    assert result.is_error
    assert "rejected the auth token" in _text(result)


def test_redirect_names_the_address_to_use(env, sentry):
    sentry.route("GET", r"/api/0/", (301, {}, {"Location": "https://sentry.lyan.test/api/0/"}))

    async def call(session):
        return await session.call_tool("sentry_info", {})

    result = _session(env, call)
    assert result.is_error
    assert "Set SENTRY_URL to https://sentry.lyan.test." in _text(result)


def test_missing_configuration_is_reported_not_crashed(env):
    broken = {k: v for k, v in env.items() if k != "SENTRY_URL"}

    async def call(session):
        return await session.call_tool("sentry_info", {})

    result = _session(broken, call)
    assert result.is_error
    assert "SENTRY_URL is not set" in _text(result)


def test_missing_token_points_at_set_token(env):
    no_token = {k: v for k, v in env.items() if k != "SENTRY_AUTH_TOKEN"}

    async def call(session):
        return await session.call_tool("search_issues", {})

    result = _session(no_token, call)
    assert result.is_error
    assert "sentry-mcp set-token" in _text(result)


def test_unreachable_server_is_explained(env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]

    async def call(session):
        return await session.call_tool("sentry_info", {})

    result = _session({**env, "SENTRY_URL": f"http://127.0.0.1:{dead_port}"}, call)
    assert result.is_error
    assert "Could not reach Sentry" in _text(result)


def test_check_command_prints_user_and_latest_issues(env):
    done = subprocess.run([*server_command(), "check"], env={**os.environ, **env}, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    info = json.loads(done.stdout)
    assert info["user"]["email"] == "keivan@lyan.test"
    assert info["latest_unresolved_24h"][0]["short_id"] == "API-1A"
    assert "missing_scopes" not in info
