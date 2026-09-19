"""`sentry-mcp` command: run the server (default), store the auth token, or test the connection."""

import argparse
import getpass
import json
import logging
import os
import sys

from sentry_mcp.config import KEYRING_SERVICE, READ_SCOPES, WRITE_SCOPES, ConfigError, Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sentry-mcp", description="Local MCP server for a self-hosted Sentry.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the MCP server on stdio (what Claude starts). Default.")
    for name, help_text in (
        ("set-token", "Store a Sentry auth token in the system credential store (Windows Credential Manager / macOS Keychain)."),
        ("delete-token", "Remove the stored token."),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--url", help="Your Sentry address, e.g. https://sentry.company.com. Defaults to SENTRY_URL.")
    commands.add_parser("check", help="Connect with the current SENTRY_* variables and print who you are and the latest issues.")
    args = parser.parse_args(argv)

    # stdout belongs to the MCP protocol; logs go to stderr (the MCP client keeps them in its log files).
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("SENTRY_MCP_LOG_LEVEL", "WARNING").upper())
    if hasattr(sys.stdout, "reconfigure") and args.command not in (None, "serve"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.command in (None, "serve"):
        from sentry_mcp.server import build_server

        build_server().run("stdio")
    elif args.command == "set-token":
        _set_token(_settings(args.url))
    elif args.command == "delete-token":
        _delete_token(_settings(args.url))
    elif args.command == "check":
        sys.exit(_check(_settings(None)))


def _settings(url: str | None) -> Settings:
    try:
        return Settings.from_env(url)
    except ConfigError as exc:
        sys.exit(f"{exc}\nPass --url or set SENTRY_URL." if "SENTRY_URL" in str(exc) else str(exc))


def _set_token(settings: Settings) -> None:
    import keyring

    print(f"Create a User Auth Token at {settings.url}/settings/account/api/auth-tokens/")
    print(f"with the scopes: {' '.join(READ_SCOPES + WRITE_SCOPES)}")
    token = getpass.getpass(f"Auth token for {settings.host}: ").strip()
    if not token:
        sys.exit("Empty token; nothing stored.")
    if token.startswith("sntrys_"):
        print("Warning: this is an organization token (sntrys_); it can only upload releases and source maps. Use a User Auth Token.")
    keyring.set_password(KEYRING_SERVICE, settings.host, token)
    print(f"Stored in the credential store (service {KEYRING_SERVICE!r}, account {settings.host!r}).")
    if os.environ.get("SENTRY_AUTH_TOKEN"):
        print("Note: SENTRY_AUTH_TOKEN is set in this environment and takes precedence over the stored token.")
    _check(settings, issues=False)


def _delete_token(settings: Settings) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, settings.host)
    except PasswordDeleteError:
        sys.exit(f"No stored token for {settings.host!r}.")
    print(f"Removed the stored token for {settings.host!r}.")


def _check(settings: Settings, issues: bool = True) -> int:
    from sentry_mcp.client import SentryClient
    from sentry_mcp.server import explain

    try:
        client = SentryClient(settings)
        info = client.info()
        if issues and info.get("default_org"):
            found = client.search_issues(period="24h", limit=3)["issues"]
            info["latest_unresolved_24h"] = [{k: i.get(k) for k in ("short_id", "title", "events", "last_seen")} for i in found]
    except Exception as exc:
        print(f"FAILED: {explain(exc, settings)}", file=sys.stderr)
        return 1
    missing = [s for s in READ_SCOPES + WRITE_SCOPES if s not in info.get("scopes", [])]
    if info.get("scopes") and missing:
        info["missing_scopes"] = missing
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0
