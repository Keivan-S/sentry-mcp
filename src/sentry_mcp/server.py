"""MCP tool surface. Every tool is a thin wrapper over SentryClient; errors come back to the model as readable text."""

import functools
import logging
from typing import Annotated, Literal

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from sentry_mcp import __version__
from sentry_mcp.client import SentryClient, SentryError
from sentry_mcp.config import READ_SCOPES, WRITE_SCOPES, ConfigError, Settings, normalize_url

log = logging.getLogger(__name__)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
TRIAGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
COMMENT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

Org = Annotated[str | None, Field(description="Organization slug. Empty = the default (SENTRY_ORG, or the only organization the token sees).")]
Issue = Annotated[str, Field(description="Issue id (4512), short id (API-1A) or the issue's URL.")]
Projects = Annotated[list[str] | None, Field(description="Project slugs or ids. Empty = every project you can access.")]
Environments = Annotated[list[str] | None, Field(description="e.g. [\"production\"]. Empty = all environments.")]
Period = Annotated[str | None, Field(description="Relative window: 30m, 24h, 14d, 90d... Ignored when start/end are given.")]
Start = Annotated[str | None, Field(description="ISO 8601 UTC, e.g. 2026-09-19T06:30:00. Overrides period.")]
End = Annotated[str | None, Field(description="ISO 8601 UTC. Default: now.")]
Cursor = Annotated[str | None, Field(description="next_cursor from the previous page.")]
MaxFrames = Annotated[int, Field(ge=5, le=200, description="Stack frames per exception after collapsing library runs.")]
Breadcrumbs = Annotated[int, Field(ge=0, le=200, description="How many of the last breadcrumbs (logs, SQL, HTTP calls) to include.")]


def explain(exc: Exception, settings: Settings | None = None) -> str:
    """Turn an exception into something the user can act on."""
    where = settings.url if settings else "Sentry"
    if isinstance(exc, ConfigError):
        return f"Configuration problem: {exc}"
    if isinstance(exc, SentryError):
        if exc.status == 401:
            return (
                f"Sentry rejected the auth token (401: {exc.detail}). Create a User Auth Token at "
                f"{where}/settings/account/api/auth-tokens/ and store it with `sentry-mcp set-token`."
            )
        if exc.status == 403:
            return (
                f"The token may not do this (403: {exc.detail}). It needs the scopes {', '.join(READ_SCOPES + WRITE_SCOPES)} "
                "and access to the project."
            )
        if exc.status == 404:
            return f"Not found (404: {exc.detail}). Check the organization/project slug or the issue/event id (list_projects, search_issues)."
        if exc.status == 429:
            return "Sentry is rate-limiting these requests (429). Wait a moment and retry, with a smaller limit."
        if 300 <= exc.status < 400:
            try:
                target = normalize_url(exc.location or "")
            except ConfigError:
                target = exc.location
            return f"Sentry redirected to {exc.location}. Set SENTRY_URL to {target}."
        if exc.status >= 500:
            return f"Sentry failed with an internal error ({exc.status}): {exc.detail}"
        return f"Sentry refused the request ({exc.status}): {exc.detail}"
    if isinstance(exc, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
        return (
            f"The TLS certificate of {where} is not trusted. Point SENTRY_CA_BUNDLE at the CA certificate (PEM), "
            "or as a last resort set SENTRY_VERIFY_SSL=false."
        )
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return f"Could not reach Sentry at {where}: {exc}. Check the network/VPN and SENTRY_URL."
    if isinstance(exc, requests.exceptions.JSONDecodeError):
        return f"{where} answered with something that is not Sentry's JSON API. Is SENTRY_URL the address you open Sentry at?"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _instructions(settings: Settings | None) -> str:
    if settings is None:
        return "Self-hosted Sentry tools. The server is not configured yet; any tool call returns the configuration error."
    writes = (
        "Read-only mode (SENTRY_READ_ONLY): issues can't be changed from here."
        if settings.read_only
        else "update_issues and add_issue_comment change what the whole team sees: use them when the user asks, and report what changed."
    )
    return (
        f"Direct connection to the self-hosted Sentry at {settings.url} (default organization: {settings.org or 'auto'}).\n"
        "- search_issues `query` is Sentry search syntax: `is:unresolved level:error`, `release:1.4.2`, `assigned:me`, "
        "`!has:assignee`, `firstSeen:-24h`, `times_seen:>100`, `user.email:a@b.com`, `url:*checkout*`; bare words match "
        "the title. An empty query also returns resolved and archived issues.\n"
        "- Issues are referred to by id, short id (API-1A) or URL.\n"
        "- get_issue returns an event with its stack trace crash point first: frames marked `app` are the project's own code, "
        "with source lines; exceptions[0] is the exception that surfaced, later entries are what caused it.\n"
        "- search_events runs Discover queries for counts and group-bys, e.g. fields [\"url\", \"count()\"] sorted by -count().\n"
        "- Timestamps are UTC.\n"
        f"- {writes}"
    )


def build_server() -> MCPServer:
    try:
        settings = Settings.from_env()
        client = SentryClient(settings)
        startup_error = None
    except Exception as exc:  # keep serving so the model can tell the user what to fix
        log.error("sentry-mcp is not configured: %s", exc)
        settings, client, startup_error = None, None, exc

    mcp = MCPServer(name="sentry", title="Sentry (self-hosted)", version=__version__, instructions=_instructions(settings))

    def tool(annotations: ToolAnnotations):
        """Register a tool whose failures reach the model as a readable ToolError instead of an opaque crash."""

        def decorate(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if startup_error is not None:
                    raise ToolError(explain(startup_error))
                try:
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except Exception as exc:
                    if isinstance(exc, ConfigError) or (isinstance(exc, SentryError) and exc.status == 401):
                        client.reset()  # a token fixed meanwhile is picked up on the next call
                    log.warning("%s failed: %s", fn.__name__, exc, exc_info=log.isEnabledFor(logging.DEBUG))
                    raise ToolError(explain(exc, settings)) from exc

            return mcp.tool(annotations=annotations)(wrapper)

        return decorate

    @tool(READ)
    def sentry_info() -> dict:
        """Connection check: the Sentry address, who the token belongs to, its scopes, the organizations and the default one."""
        return client.info()

    @tool(READ)
    def list_projects(org: Org = None) -> dict:
        """Projects of the organization: slug, id, platform, teams."""
        return client.list_projects(org)

    @tool(READ)
    def list_teams(org: Org = None) -> dict:
        """Teams of the organization (issues can be assigned to a team as "#slug")."""
        return client.list_teams(org)

    @tool(READ)
    def find_members(
        query: Annotated[str | None, Field(description="Part of a name, username or email. Empty lists everyone.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        org: Org = None,
    ) -> dict:
        """Organization members: user id, name, email, role."""
        return client.find_members(query, org, limit)

    @tool(READ)
    def search_issues(
        query: Annotated[str | None, Field(description="Sentry search syntax, e.g. 'is:unresolved level:error release:1.4.2'. Empty = every status.")] = "is:unresolved",
        projects: Projects = None,
        environments: Environments = None,
        period: Period = "14d",
        start: Start = None,
        end: End = None,
        sort: Annotated[
            Literal["date", "new", "freq", "user", "trends"],
            Field(description="date = last seen, new = first seen, freq = events, user = users affected, trends = rising."),
        ] = "date",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: Cursor = None,
        org: Org = None,
    ) -> dict:
        """Find issues (grouped errors) seen in the period: id, short id, title, culprit, level, status, events, users, first/last seen, assignee."""
        return client.search_issues(query, projects, environments, period, start, end, sort, limit, cursor, org)

    @tool(READ)
    def get_issue(
        issue: Issue,
        event: Annotated[str | None, Field(description='"latest", "oldest", "recommended", an event id, or empty for no event.')] = "latest",
        max_frames: MaxFrames = 40,
        breadcrumbs: Breadcrumbs = 30,
        org: Org = None,
    ) -> dict:
        """One issue in full: status, releases, tag breakdown, comments and activity, plus one event with its stack trace, request, breadcrumbs, user and contexts."""
        return client.get_issue(issue, event, max_frames, breadcrumbs, org)

    @tool(READ)
    def list_issue_events(
        issue: Issue,
        query: Annotated[str | None, Field(description="Filter the events, e.g. 'environment:production user.email:a@b.com'.")] = None,
        environments: Environments = None,
        period: Period = None,
        start: Start = None,
        end: End = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        cursor: Cursor = None,
        org: Org = None,
    ) -> dict:
        """The individual occurrences of an issue, newest first: event id, time, release, environment, user, URL."""
        return client.list_issue_events(issue, query, environments, period, start, end, limit, cursor, org)

    @tool(READ)
    def get_event(
        event_id: Annotated[str, Field(description="32-character event id (dashes allowed).")],
        issue: Annotated[str | None, Field(description="The event's issue, if known.")] = None,
        project: Annotated[str | None, Field(description="The event's project slug, if known.")] = None,
        max_frames: MaxFrames = 40,
        breadcrumbs: Breadcrumbs = 30,
        org: Org = None,
    ) -> dict:
        """One event in full by its id (from list_issue_events, a log line or a user report); the project is looked up when not given."""
        return client.get_event(event_id, issue, project, max_frames, breadcrumbs, org)

    @tool(READ)
    def get_issue_tag_values(
        issue: Issue,
        key: Annotated[str, Field(description="Tag key: url, release, environment, user, browser, os, server_name, transaction, or a custom tag.")],
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: Cursor = None,
        org: Org = None,
    ) -> dict:
        """How an issue's events split over one tag, most frequent first: which URLs, releases, users or servers are hit."""
        return client.issue_tag_values(issue, key, limit, cursor, org)

    @tool(READ)
    def search_events(
        fields: Annotated[
            list[str],
            Field(
                min_length=1,
                description='Columns and aggregates, e.g. ["title", "count()", "count_unique(user)", "last_seen()"] or '
                '["transaction", "p95(transaction.duration)"] with dataset "transactions".',
            ),
        ],
        query: Annotated[str | None, Field(description="Sentry search syntax over events, e.g. 'level:error url:*reports*'.")] = "",
        dataset: Annotated[str, Field(description='"errors", "transactions", or "discover" (both).')] = "errors",
        sort: Annotated[str | None, Field(description='A field or aggregate from `fields`, "-" for descending, e.g. "-count()".')] = None,
        projects: Projects = None,
        environments: Environments = None,
        period: Period = "24h",
        start: Start = None,
        end: End = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        cursor: Cursor = None,
        org: Org = None,
    ) -> dict:
        """Discover query over raw events: counts and group-bys that search_issues can't answer (errors per URL, per release, per user; slow transactions)."""
        return client.search_events(fields, query, dataset, sort, projects, environments, period, start, end, limit, cursor, org)

    @tool(READ)
    def list_releases(
        projects: Projects = None,
        query: Annotated[str | None, Field(description="Part of a version string.")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        org: Org = None,
    ) -> dict:
        """Releases, newest first: version, dates, new issues, projects, last deploy."""
        return client.list_releases(projects, query, limit, org)

    if settings is None or not settings.read_only:

        @tool(TRIAGE)
        def update_issues(
            issues: Annotated[list[str], Field(min_length=1, max_length=100, description="Issue ids, short ids or URLs.")],
            status: Annotated[
                Literal["resolved", "resolved_in_next_release", "unresolved", "archived"] | None,
                Field(description='"archived" alone archives until the issue escalates; see the archive_* options.'),
            ] = None,
            archive_minutes: Annotated[int | None, Field(ge=1, description="Archived only: reopen after this many minutes.")] = None,
            archive_until_count: Annotated[int | None, Field(ge=1, description="Archived only: reopen after this many more events.")] = None,
            archive_forever: Annotated[bool, Field(description="Archived only: never reopen on its own.")] = False,
            assign_to: Annotated[str | None, Field(description='"me", an email / username / name, "#team-slug", or "none" to unassign.')] = None,
            priority: Literal["high", "medium", "low"] | None = None,
            bookmark: bool | None = None,
            seen: Annotated[bool | None, Field(description="Mark as seen (true) or unseen (false) for you.")] = None,
            subscribe: Annotated[bool | None, Field(description="Get (true) or stop (false) workflow notifications for the issue.")] = None,
            org: Org = None,
        ) -> dict:
            """Change issues: resolve, resolve in next release, reopen, archive, assign, set priority, bookmark, mark seen, subscribe."""
            return client.update_issues(
                issues, status, archive_minutes, archive_until_count, archive_forever, assign_to, priority, bookmark, seen, subscribe, org
            )

        @tool(COMMENT)
        def add_issue_comment(
            issue: Issue,
            text: Annotated[str, Field(min_length=1, description="Markdown text shown in the issue's activity.")],
            org: Org = None,
        ) -> dict:
            """Post a comment on an issue (visible to the team in its activity feed)."""
            return client.add_comment(issue, text, org)

    return mcp
