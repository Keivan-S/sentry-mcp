# sentry-mcp

[فارسی](README.fa.md)

A local MCP server that connects **Claude Desktop** (and Claude Code) directly to a **self-hosted Sentry**.

It runs on your own machine and talks straight to your Sentry's web API (`/api/0/`). There is no cloud relay, and no
second LLM either: the official Sentry MCP server hands issue/event search to OpenAI or Anthropic through its own API key,
while here Claude writes the Sentry search syntax itself. If your PC can open Sentry in a browser, this server can reach it too.

## Tools

| Tool | What it does | Writes? |
|---|---|---|
| `sentry_info` | Connection check: address, token owner, scopes, organizations | no |
| `list_projects` / `list_teams` / `find_members` | Who and what exists in the organization | no |
| `search_issues` | Issues by Sentry search syntax (`is:unresolved level:error release:1.4.2`), period, project, environment, sort | no |
| `get_issue` | Full issue: status, releases, tag breakdown, comments/activity, plus one event (latest / oldest / recommended / by id) | no |
| `list_issue_events` | The individual occurrences of an issue | no |
| `get_event` | One event by id; the project is looked up when not given | no |
| `get_issue_tag_values` | How an issue splits over a tag: URLs, releases, users, servers | no |
| `search_events` | Discover queries: counts and group-bys (`["url", "count()"]`, `p95(transaction.duration)`) | no |
| `list_releases` | Releases with new-issue counts and last deploy | no |
| `update_issues` | Resolve / resolve in next release / reopen / archive, assign (`me`, email, `#team`), priority, bookmark, seen, subscribe | issues |
| `add_issue_comment` | Comment on an issue | issues |

`SENTRY_READ_ONLY=true` leaves the two write tools out.

Events come back shaped for reading: the crash point first, the app's own frames with their source lines (and variables
when the SDK sends them), long runs of library frames collapsed, the chained exceptions in surfaced-first order, the
request without cookies or auth headers, the last breadcrumbs (SQL, logs, HTTP calls), user, tags and contexts.
Issues can be referred to by id, short id (`API-1A`) or URL. Timestamps are UTC.

## The auth token

Create a **User Auth Token** in Sentry: *User settings → Personal Tokens* (`<sentry>/settings/account/api/auth-tokens/`),
with these scopes:

- Read: `org:read`, `project:read`, `team:read`, `member:read`, `event:read`
- Write (for `update_issues` / `add_issue_comment`): `event:write`

An organization token (`sntrys_…`) is not enough; it can only upload releases and source maps.

## Install from a release (recommended)

Download the file for your machine from [Releases](../../releases):

| Machine | File |
|---|---|
| Windows | `sentry-mcp-<version>-windows-x64.mcpb` |
| Mac, Apple silicon (M1 and later) | `sentry-mcp-<version>-macos-arm64.mcpb` |
| Mac, Intel | `sentry-mcp-<version>-macos-x64.mcpb` |

1. Double-click the `.mcpb` file (or install it from Claude Desktop → Settings → Extensions).
2. Fill in the Sentry address (e.g. `https://sentry.company.com`) and the auth token.
3. Claude Desktop keeps the token in the operating system's secure storage, not in a config file.

No Python needed; the executable is self-contained. The standalone executables are attached to the release as well.
The binaries are not code-signed: Windows may show a SmartScreen warning. For the standalone macOS executable run once:

```bash
xattr -d com.apple.quarantine sentry-mcp-*-macos-* && chmod +x sentry-mcp-*-macos-*
```

## Install from source

```powershell
cd D:\Projects\Packages\sentry-mcp
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Store the token once, in **Windows Credential Manager** (macOS: Keychain). It prompts for the token without echoing it,
then checks it against Sentry and prints who it belongs to and any missing scopes:

```powershell
.\.venv\Scripts\sentry-mcp.exe set-token --url https://sentry.company.com
```

Test the connection. It prints the token owner, the organizations and the latest unresolved issues, or a clear reason why it failed:

```powershell
$env:SENTRY_URL = "https://sentry.company.com"
.\.venv\Scripts\sentry-mcp.exe check
```

Claude Code:

```powershell
claude mcp add sentry -s user -e SENTRY_URL=https://sentry.company.com -- D:\Projects\Packages\sentry-mcp\.venv\Scripts\sentry-mcp.exe
```

Claude Desktop, in `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sentry": {
      "command": "D:\\Projects\\Packages\\sentry-mcp\\.venv\\Scripts\\sentry-mcp.exe",
      "env": { "SENTRY_URL": "https://sentry.company.com" }
    }
  }
}
```

Restart Claude Desktop afterwards (quit it from the tray icon; closing the window is not enough).

## Settings

The names match sentry-cli's, so one set of variables serves both.

| Variable | Default | Meaning |
|---|---|---|
| `SENTRY_URL` | (required) | The address you open Sentry at, e.g. `https://sentry.company.com` |
| `SENTRY_AUTH_TOKEN` | credential store | Plain-text fallback for the token |
| `SENTRY_ORG` | the only one | Default organization slug, when the token sees several |
| `SENTRY_READ_ONLY` | `false` | `true` leaves out `update_issues` and `add_issue_comment` |
| `SENTRY_USE_SYSTEM_CERTS` | `true` | Trust the OS certificate store (internal CAs) |
| `SENTRY_CA_BUNDLE` | none | PEM file of an internal CA the OS does not trust |
| `SENTRY_VERIFY_SSL` | `true` | `false` disables TLS verification (last resort) |
| `SENTRY_TIMEOUT` | `30` | Seconds per request |
| `SENTRY_MCP_LOG_LEVEL` | `WARNING` | stderr log level (Claude Desktop: `mcp-server-sentry.log` in its logs folder) |

Redirects are not followed, so a token is never replayed to another address. If Sentry redirects (e.g. `http://` →
`https://`), the error names the address to put in `SENTRY_URL`.

## Development

```bash
pip install -e ".[dev,build]"
pytest                        # runs against a fake Sentry API (tests/fake_sentry.py)
python packaging/build.py     # standalone executable + .mcpb for this platform, in dist/
```

Pushing a `v*` tag makes GitHub Actions build Windows x64, macOS arm64 and macOS x64, run the MCP tests against each
built executable, and attach everything to a GitHub release.
