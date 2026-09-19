"""Direct client for Sentry's web API (/api/0/). No relay and no second LLM: the model writes Sentry search syntax itself."""

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from sentry_mcp import __version__, render
from sentry_mcp.config import Settings, resolve_token

log = logging.getLogger(__name__)

ISSUE_URL = re.compile(r"/issues/(\d+)")
SHORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*-[A-Za-z0-9]+")
PERIOD = re.compile(r"\d+[smhdw]")
LINK = re.compile(r'<[^>]*>;\s*rel="(?P<rel>\w+)";\s*results="(?P<results>\w+)";\s*cursor="(?P<cursor>[^"]*)"')
UNASSIGN = {"", "none", "nobody", "unassign", "unassigned"}


class SentryError(Exception):
    """Sentry answered with an error status; `detail` is Sentry's own explanation."""

    def __init__(self, status: int, detail: str, method: str, path: str, location: str | None = None):
        super().__init__(f"{method} /api/0/{path} -> {status}: {detail}")
        self.status = status
        self.detail = detail
        self.location = location


def next_cursor(link: str | None) -> str | None:
    """Sentry pages through a Link header; the next page exists only when its `results` is "true"."""
    for part in LINK.finditer(link or ""):
        if part["rel"] == "next" and part["results"] == "true":
            return part["cursor"]
    return None


def status_body(status: str | None, archive_minutes: int | None, archive_until_count: int | None, archive_forever: bool) -> dict:
    """The PUT body for a status change. Archiving without an option means "until it escalates", like the UI."""
    archive_options = sum(bool(x) for x in (archive_minutes, archive_until_count, archive_forever))
    if status != "archived" and archive_options:
        raise ValueError("archive_minutes / archive_until_count / archive_forever only go with status='archived'.")
    if status is None:
        return {}
    if status == "resolved":
        return {"status": "resolved", "statusDetails": {}}
    if status == "resolved_in_next_release":
        return {"status": "resolved", "statusDetails": {"inNextRelease": True}}
    if status == "unresolved":
        return {"status": "unresolved", "statusDetails": {}}
    if status == "archived":
        if archive_options > 1:
            raise ValueError("Pick one of archive_minutes, archive_until_count, archive_forever.")
        if archive_minutes:
            return {"status": "ignored", "substatus": "archived_until_condition_met", "statusDetails": {"ignoreDuration": archive_minutes}}
        if archive_until_count:
            return {"status": "ignored", "substatus": "archived_until_condition_met", "statusDetails": {"ignoreCount": archive_until_count}}
        if archive_forever:
            return {"status": "ignored", "substatus": "archived_forever", "statusDetails": {}}
        return {"status": "ignored", "substatus": "archived_until_escalating", "statusDetails": {}}
    raise ValueError(f"Unknown status {status!r}.")


def time_range(period: str | None, start: str | None, end: str | None) -> dict:
    if start or end:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return {"start": start or "1970-01-01T00:00:00", "end": end or now}
    if not period:
        return {}
    if not PERIOD.fullmatch(period.strip()):
        raise ValueError(f"period must look like 30m, 24h, 14d or 2w (got {period!r}).")
    return {"statsPeriod": period.strip()}


def _seg(value: Any) -> str:
    return quote(str(value).strip(), safe="")


def _detail(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()[:300] or resp.reason or "no details"
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
        return str(detail.get("message") or detail) if isinstance(detail, dict) else str(detail)
    if isinstance(body, dict):  # field validation errors: {"field": ["message", ...]}
        return "; ".join(f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in body.items())[:500]
    if isinstance(body, list):
        return "; ".join(map(str, body))[:500]
    return str(body)[:500]


def _configure_tls(settings: Settings) -> None:
    if settings.use_system_certs:
        # Trust whatever the OS trusts, so an internal CA that already works in the browser works here too.
        import truststore

        truststore.inject_into_ssl()


class SentryClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._session: requests.Session | None = None
        self._default_org = settings.org
        self._projects: dict[str, list[dict]] = {}
        self._teams: dict[str, list[dict]] = {}
        self._root: dict | None = None

    def reset(self) -> None:
        """Forget the session and caches, so the next call re-reads the token (e.g. after `sentry-mcp set-token`)."""
        with self._lock:
            if self._session is not None:
                self._session.close()
            self._session = None
            self._default_org = self.settings.org
            self._projects.clear()
            self._teams.clear()
            self._root = None

    # --- transport -------------------------------------------------------------------------------------------------

    def _http(self) -> requests.Session:
        with self._lock:
            if self._session is None:
                _configure_tls(self.settings)
                session = requests.Session()
                session.headers.update(
                    {
                        "Authorization": f"Bearer {resolve_token(self.settings)}",
                        "User-Agent": f"sentry-mcp/{__version__}",
                        "Accept": "application/json",
                    }
                )
                session.verify = self.settings.ca_bundle or self.settings.verify_ssl
                self._session = session
            return self._session

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> requests.Response:
        url = f"{self.settings.url}/api/0/{path}"
        log.debug("%s %s %s", method, url, params)
        # Redirects are not followed: a PUT turned into a GET, or a token sent to another host, would be worse than an error.
        resp = self._http().request(method, url, params=params, json=body, timeout=self.settings.timeout, allow_redirects=False)
        if 300 <= resp.status_code < 400:
            raise SentryError(resp.status_code, "redirect", method, path, location=resp.headers.get("Location"))
        if resp.status_code >= 400:
            raise SentryError(resp.status_code, _detail(resp), method, path)
        return resp

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._call("GET", path, params).json()

    def _get_page(self, path: str, params: dict) -> tuple[Any, str | None]:
        resp = self._call("GET", path, params)
        return resp.json(), next_cursor(resp.headers.get("Link"))

    def _get_all(self, path: str, params: dict | None = None, cap: int = 2000) -> list:
        items: list = []
        cursor = None
        while True:
            page, cursor = self._get_page(path, {**(params or {}), **({"cursor": cursor} if cursor else {})})
            items.extend(page)
            if not cursor or len(items) >= cap:
                return items

    # --- lookups ---------------------------------------------------------------------------------------------------

    def _whoami(self) -> dict:
        with self._lock:
            if self._root is None:
                self._root = self._get("")
            return self._root

    def org(self, org: str | None = None) -> str:
        if org and org.strip():
            return org.strip()
        with self._lock:
            if self._default_org:
                return self._default_org
        orgs = self._get("organizations/", {"member": "1"})
        if len(orgs) == 1:
            with self._lock:
                self._default_org = orgs[0]["slug"]
            return orgs[0]["slug"]
        if not orgs:
            raise ValueError("This token sees no organization. Check that its user is a member of one.")
        slugs = ", ".join(o["slug"] for o in orgs)
        raise ValueError(f"The token sees several organizations ({slugs}); pass `org` or set SENTRY_ORG.")

    def _project_list(self, org: str) -> list[dict]:
        with self._lock:
            cached = self._projects.get(org)
        if cached is None:
            cached = self._get_all(f"organizations/{_seg(org)}/projects/", {"per_page": 100})
            with self._lock:
                self._projects[org] = cached
        return cached

    def _team_list(self, org: str) -> list[dict]:
        with self._lock:
            cached = self._teams.get(org)
        if cached is None:
            cached = self._get_all(f"organizations/{_seg(org)}/teams/", {"per_page": 100})
            with self._lock:
                self._teams[org] = cached
        return cached

    def _project_ids(self, org: str, projects: list[str] | None) -> list[int]:
        """Slugs or ids -> ids. None/empty means every project the token can access (-1), not just "my teams'"."""
        if not projects:
            return [-1]
        known: dict[str, dict] | None = None
        ids = []
        for ref in projects:
            ref = str(ref).strip()
            if ref.lstrip("-").isdigit():
                ids.append(int(ref))
                continue
            known = known or {p["slug"]: p for p in self._project_list(org)}
            if ref not in known:
                raise ValueError(f"No project {ref!r} in {org}. Projects: {', '.join(sorted(known)) or 'none visible'}.")
            ids.append(int(known[ref]["id"]))
        return ids

    def issue_id(self, org: str, issue: str) -> str:
        """Numeric id, short id (PROJECT-1A) or an issue URL -> numeric id."""
        ref = str(issue).strip()
        if match := ISSUE_URL.search(ref):
            return match.group(1)
        if ref.isdigit():
            return ref
        if SHORT_ID.fullmatch(ref):
            return str(self._get(f"organizations/{_seg(org)}/shortids/{_seg(ref)}/")["groupId"])
        raise ValueError(f"{issue!r} is not an issue id, a short id like PROJECT-1A, or an issue URL.")

    def _assignee(self, org: str, who: str) -> str | None:
        """"me", email/username/name, "#team", "team:<slug|id>", "user:<id>" or "none" -> Sentry's actor string (None unassigns)."""
        ref = who.strip()
        if ref.lower() in UNASSIGN:
            return None
        if ref.lower() == "me":
            user = self._whoami().get("user") or {}
            if not user.get("id"):
                raise ValueError("This token is not tied to a user, so 'me' can't be resolved; use an email instead.")
            return f"user:{user['id']}"
        if re.fullmatch(r"(user|team):\d+", ref):
            return ref
        team = ref[1:] if ref.startswith("#") else ref[5:] if ref.lower().startswith("team:") else None
        if team is not None:
            teams = self._team_list(org)
            matches = [t for t in teams if team.lower() in (t["slug"].lower(), str(t.get("name", "")).lower())]
            if len(matches) == 1:
                return f"team:{matches[0]['id']}"
            raise ValueError(f"No team {team!r}. Teams: {', '.join('#' + t['slug'] for t in teams) or 'none'}.")

        members = [m for m in self._get(f"organizations/{_seg(org)}/members/", {"query": ref, "per_page": 20}) if m.get("user")]
        wanted = ref.lower()
        exact = [
            m
            for m in members
            if wanted in {str(v).lower() for v in (m.get("email"), m["user"].get("email"), m["user"].get("username"), m["user"].get("name"), m.get("name")) if v}
        ]
        pick = exact if len(exact) == 1 else members if len(members) == 1 else []
        if pick:
            return f"user:{pick[0]['user']['id']}"
        if not members:
            raise ValueError(f"No organization member matches {who!r}. Use find_members to look people up.")
        emails = ", ".join(m.get("email") or m["user"].get("email") or m["user"].get("username", "?") for m in members)
        raise ValueError(f"{who!r} matches several members ({emails}); use the exact email.")

    # --- read ------------------------------------------------------------------------------------------------------

    def list_organizations(self) -> dict:
        return {"organizations": [render.organization(o) for o in self._get("organizations/", {"member": "1"})]}

    def info(self) -> dict:
        root = self._whoami()
        orgs = self.list_organizations()["organizations"]
        try:
            default_org = self.org()
        except ValueError:
            default_org = None
        return render.compact(
            {
                "url": self.settings.url,
                "user": render.person(root.get("user")),
                "scopes": sorted((root.get("auth") or {}).get("scopes") or []),
                "organizations": orgs,
                "default_org": default_org,
                "read_only": self.settings.read_only,
            }
        )

    def list_projects(self, org: str | None = None) -> dict:
        org = self.org(org)
        return {"org": org, "projects": [render.project(p) for p in self._project_list(org)]}

    def list_teams(self, org: str | None = None) -> dict:
        org = self.org(org)
        return {"org": org, "teams": [render.team(t) for t in self._team_list(org)]}

    def find_members(self, query: str | None = None, org: str | None = None, limit: int = 50) -> dict:
        org = self.org(org)
        params: dict = {"per_page": limit}
        if query:
            params["query"] = query
        return {"org": org, "members": [render.member(m) for m in self._get(f"organizations/{_seg(org)}/members/", params)]}

    def search_issues(
        self,
        query: str | None = "is:unresolved",
        projects: list[str] | None = None,
        environments: list[str] | None = None,
        period: str | None = "14d",
        start: str | None = None,
        end: str | None = None,
        sort: str = "date",
        limit: int = 25,
        cursor: str | None = None,
        org: str | None = None,
    ) -> dict:
        org = self.org(org)
        params: dict = {
            "query": query or "",  # sent even when empty: without it Sentry falls back to is:unresolved
            "project": self._project_ids(org, projects),
            "sort": sort,
            "limit": limit,
            "shortIdLookup": 1,
            **time_range(period, start, end),
        }
        if environments:
            params["environment"] = environments
        if cursor:
            params["cursor"] = cursor
        groups, after = self._get_page(f"organizations/{_seg(org)}/issues/", params)
        return render.compact({"org": org, "query": params["query"], "issues": [render.issue(g) for g in groups], "next_cursor": after}, keep=("issues",))

    def get_issue(self, issue: str, event: str | None = "latest", max_frames: int = 40, breadcrumbs: int = 30, org: str | None = None) -> dict:
        org = self.org(org)
        base = f"organizations/{_seg(org)}/issues/{self.issue_id(org, issue)}/"
        out: dict = {"issue": render.issue_detail(self._get(base))}
        try:
            out["tags"] = render.tag_summary(self._get(base + "tags/", {"limit": 5}))
        except SentryError as exc:  # the breakdown is a bonus; the issue itself already loaded
            log.info("tag summary unavailable: %s", exc)
        if event:
            out["event"] = render.event(self._get(base + f"events/{_seg(event)}/"), max_frames, breadcrumbs)
        return out

    def list_issue_events(
        self,
        issue: str,
        query: str | None = None,
        environments: list[str] | None = None,
        period: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        org: str | None = None,
    ) -> dict:
        org = self.org(org)
        gid = self.issue_id(org, issue)
        params: dict = {"full": "false", "per_page": limit, **time_range(period, start, end)}
        if query:
            params["query"] = query
        if environments:
            params["environment"] = environments
        if cursor:
            params["cursor"] = cursor
        events, after = self._get_page(f"organizations/{_seg(org)}/issues/{gid}/events/", params)
        return render.compact({"issue_id": gid, "events": [render.event_summary(e) for e in events], "next_cursor": after}, keep=("events",))

    def get_event(
        self,
        event_id: str,
        issue: str | None = None,
        project: str | None = None,
        max_frames: int = 40,
        breadcrumbs: int = 30,
        org: str | None = None,
    ) -> dict:
        org = self.org(org)
        eid = event_id.strip().replace("-", "").lower()
        if issue:
            ev = self._get(f"organizations/{_seg(org)}/issues/{self.issue_id(org, issue)}/events/{_seg(eid)}/")
        elif project:
            ev = self._get(f"projects/{_seg(org)}/{_seg(project)}/events/{_seg(eid)}/")
        else:
            found = self._get(f"organizations/{_seg(org)}/eventids/{_seg(eid)}/")
            ev, project = found["event"], found.get("projectSlug")
        return render.compact({"project": project, "event": render.event(ev, max_frames, breadcrumbs)})

    def issue_tag_values(self, issue: str, key: str, limit: int = 25, cursor: str | None = None, org: str | None = None) -> dict:
        org = self.org(org)
        gid = self.issue_id(org, issue)
        base = f"organizations/{_seg(org)}/issues/{gid}/tags/{_seg(key)}/"
        summary = self._get(base)
        params: dict = {"per_page": limit, "sort": "count"}  # Sentry: date | age | count (most events first)
        if cursor:
            params["cursor"] = cursor
        values, after = self._get_page(base + "values/", params)
        return render.compact(
            {
                "issue_id": gid,
                "key": summary.get("key", key),
                "events_with_tag": summary.get("totalValues"),
                "distinct_values": summary.get("uniqueValues"),
                "values": [render.tag_value(v) for v in values],
                "next_cursor": after,
            },
            keep=("values",),
        )

    def search_events(
        self,
        fields: list[str],
        query: str | None = "",
        dataset: str = "errors",
        sort: str | None = None,
        projects: list[str] | None = None,
        environments: list[str] | None = None,
        period: str | None = "24h",
        start: str | None = None,
        end: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        org: str | None = None,
    ) -> dict:
        org = self.org(org)
        params: dict = {
            "field": fields,
            "query": query or "",
            "dataset": dataset,
            "project": self._project_ids(org, projects),
            "per_page": limit,
            **time_range(period, start, end),
        }
        if sort:
            params["sort"] = sort
        if environments:
            params["environment"] = environments
        if cursor:
            params["cursor"] = cursor
        result, after = self._get_page(f"organizations/{_seg(org)}/events/", params)
        meta = result.get("meta") or {}
        return render.compact(
            {
                "rows": [render.trim(row, 500, 50, 2) for row in result.get("data") or []],
                "types": meta.get("fields"),
                "units": {k: v for k, v in (meta.get("units") or {}).items() if v},
                "next_cursor": after,
            },
            keep=("rows",),
        )

    def list_releases(self, projects: list[str] | None = None, query: str | None = None, limit: int = 20, org: str | None = None) -> dict:
        org = self.org(org)
        params: dict = {"project": self._project_ids(org, projects), "per_page": limit}
        if query:
            params["query"] = query
        return {"org": org, "releases": [render.release(r) for r in self._get(f"organizations/{_seg(org)}/releases/", params)]}

    # --- write -----------------------------------------------------------------------------------------------------

    def update_issues(
        self,
        issues: list[str],
        status: str | None = None,
        archive_minutes: int | None = None,
        archive_until_count: int | None = None,
        archive_forever: bool = False,
        assign_to: str | None = None,
        priority: str | None = None,
        bookmark: bool | None = None,
        seen: bool | None = None,
        subscribe: bool | None = None,
        org: str | None = None,
    ) -> dict:
        org = self.org(org)
        body = status_body(status, archive_minutes, archive_until_count, archive_forever)
        if assign_to is not None:
            body["assignedTo"] = self._assignee(org, assign_to)
        if priority:
            body["priority"] = priority
        for field, value in (("isBookmarked", bookmark), ("hasSeen", seen), ("isSubscribed", subscribe)):
            if value is not None:
                body[field] = value
        if not body:
            raise ValueError("Nothing to change: pass status, assign_to, priority, bookmark, seen or subscribe.")

        updated, failed, first_error = [], [], None
        for ref in issues:
            try:
                gid = self.issue_id(org, ref)
                group = self._call("PUT", f"organizations/{_seg(org)}/issues/{gid}/", body=body).json()
                updated.append(render.issue(group) if group.get("id") else {"id": gid, **group})
            except (SentryError, ValueError) as exc:
                if isinstance(exc, SentryError) and exc.status == 401:
                    raise
                first_error = first_error or exc
                failed.append({"issue": ref, "error": exc.detail if isinstance(exc, SentryError) else str(exc)})
        if not updated and first_error is not None:
            raise first_error
        return render.compact({"updated": updated, "failed": failed}, keep=("updated",))

    def add_comment(self, issue: str, text: str, org: str | None = None) -> dict:
        org = self.org(org)
        gid = self.issue_id(org, issue)
        note = self._call("POST", f"organizations/{_seg(org)}/issues/{gid}/comments/", body={"text": text}).json()
        return render.compact(
            {
                "issue_id": gid,
                "comment_id": note.get("id"),
                "date": note.get("dateCreated"),
                "text": (note.get("data") or {}).get("text"),
            }
        )
