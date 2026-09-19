"""A tiny fake Sentry API: JSON per route, every call recorded, so the real client + server stack can be exercised."""

import copy
import json
import re
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "sntryu_test_token"
ORG = "lyan"


class FakeSentry:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []
        self.token = TOKEN
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                call = {
                    "method": method,
                    "path": parts.path,
                    "query": parse_qs(parts.query, keep_blank_values=True),
                    "json": json.loads(raw) if raw else None,
                    "auth": self.headers.get("Authorization"),
                }
                fake.calls.append(call)
                if call["auth"] != f"Bearer {fake.token}":
                    return self._reply(401, {"detail": "Invalid token"})
                for route_method, pattern, handler in reversed(fake.routes):  # later registrations win
                    match = pattern.fullmatch(parts.path)
                    if route_method == method and match:
                        result = handler(call, *match.groups()) if callable(handler) else handler
                        status, body, headers = result if isinstance(result, tuple) else (200, result, {})
                        return self._reply(status, body, headers)
                self._reply(404, {"detail": "The requested resource does not exist"})

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

            def do_POST(self):
                self._handle("POST")

            def _reply(self, status, body, headers=None):
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def route(self, method: str, path: str, handler):
        """`path` is a regex over the URL path; `handler` is a JSON body, or fn(call, *groups) -> body | (status, body, headers)."""
        self.routes.append((method, re.compile(path), handler))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def last(self, method: str, path_part: str) -> dict:
        return [c for c in self.calls if c["method"] == method and path_part in c["path"]][-1]


# --- canned data --------------------------------------------------------------------------------------------------

ME = {"id": "7", "name": "Keivan", "email": "keivan@lyan.test", "username": "keivan"}
SARA = {"id": "8", "name": "Sara", "email": "sara@lyan.test", "username": "sara@lyan.test"}

PROJECTS = [
    {"id": "11", "slug": "api", "name": "api", "platform": "php-laravel", "isMember": True, "teams": [{"slug": "backend"}]},
    {"id": "12", "slug": "web", "name": "Web panel", "platform": "javascript-react", "isMember": False, "teams": []},
]
TEAMS = [{"id": "3", "slug": "backend", "name": "backend", "memberCount": 2, "isMember": True}]
MEMBERS = [
    {"id": "70", "email": ME["email"], "name": ME["name"], "orgRole": "owner", "user": ME},
    {"id": "80", "email": SARA["email"], "name": SARA["name"], "orgRole": "member", "user": SARA},
]

ISSUE = {
    "id": "4512",
    "shortId": "API-1A",
    "title": "ErrorException: Undefined array key \"brand_id\"",
    "culprit": "App\\Services\\ReportService::store",
    "permalink": "https://sentry.lyan.test/organizations/lyan/issues/4512/",
    "level": "error",
    "status": "unresolved",
    "substatus": "ongoing",
    "priority": "high",
    "count": "321",
    "userCount": 17,
    "firstSeen": "2026-09-10T08:00:00Z",
    "lastSeen": "2026-09-19T11:59:00Z",
    "project": {"id": "11", "slug": "api"},
    "metadata": {"type": "ErrorException", "value": "Undefined array key", "filename": "/app/Services/ReportService.php", "function": "store"},
    "assignedTo": None,
    "isUnhandled": True,
    "isBookmarked": False,
    "hasSeen": False,
    "isSubscribed": True,
    "numComments": 1,
    "issueType": "error",
    "firstRelease": {"version": "2026.09.10"},
    "lastRelease": {"version": "2026.09.18"},
    "statusDetails": {},
    "participants": [ME],
    "activity": [
        {"type": "note", "dateCreated": "2026-09-19T10:00:00Z", "user": ME, "data": {"text": "Looking into it"}},
        {"type": "first_seen", "dateCreated": "2026-09-10T08:00:00Z", "user": None, "data": {}},
    ],
}
SECOND_ISSUE = {**ISSUE, "id": "4513", "shortId": "WEB-2", "title": "TypeError: x is undefined", "project": {"id": "12", "slug": "web"}, "count": "5", "userCount": 2}

VENDOR = [
    {"filename": f"/vendor/laravel/framework/src/Pipeline.php", "lineNo": 100 + n, "function": "handle", "inApp": False}
    for n in range(8)
]
EVENT = {
    "eventID": "9f3c1c0d2b7a4c1e8e5f6a7b8c9d0e1f",
    "groupID": "4512",
    "dateCreated": "2026-09-19T11:59:00Z",
    "title": ISSUE["title"],
    "location": "/app/Services/ReportService.php",
    "platform": "php",
    "release": {"version": "2026.09.18"},
    "tags": [
        {"key": "level", "value": "error"},
        {"key": "environment", "value": "production"},
        {"key": "release", "value": "2026.09.18"},
        {"key": "url", "value": "https://api.lyan.test/v1/reports"},
        {"key": "server_name", "value": "api-1"},
    ],
    "user": {"id": "1953", "email": "rep@lyan.test", "ip_address": "10.0.0.5"},
    "sdk": {"name": "sentry.php.laravel", "version": "4.26.0"},
    "contexts": {"runtime": {"name": "php", "version": "8.4.3", "type": "runtime"}, "trace": {"trace_id": "abc123", "type": "trace"}},
    "context": {"job": "none"},
    "entries": [
        {
            "type": "exception",
            "data": {
                "values": [
                    {
                        "type": "PDOException",
                        "value": "SQLSTATE[23502]: Not null violation",
                        "stacktrace": {"frames": [{"filename": "/vendor/laravel/framework/src/Connection.php", "lineNo": 580, "function": "execute", "inApp": False}]},
                    },
                    {
                        "type": "ErrorException",
                        "value": "Undefined array key \"brand_id\"",
                        "mechanism": {"type": "generic", "handled": False},
                        "stacktrace": {
                            "frames": [
                                {"filename": "/public/index.php", "lineNo": 52, "function": "{main}", "inApp": True},
                                *VENDOR,
                                {
                                    "filename": "/app/Http/Controllers/ReportController.php",
                                    "lineNo": 31,
                                    "function": "store",
                                    "inApp": True,
                                    "context": [[30, "    $data = $request->validated();"], [31, "    return $this->service->store($data);"]],
                                },
                                {
                                    "filename": "/app/Services/ReportService.php",
                                    "lineNo": 88,
                                    "function": "store",
                                    "inApp": True,
                                    "context": [[87, "    $row = [];"], [88, "    $brand = $data['brand_id'];"], [89, "    return $row;"]],
                                    "vars": {"$data": {"visit_id": 5}},
                                },
                                {"filename": "/vendor/laravel/framework/src/HandleExceptions.php", "lineNo": 255, "function": "handleError", "inApp": False},
                            ]
                        },
                    },
                ]
            },
        },
        {
            "type": "request",
            "data": {
                "method": "POST",
                "url": "https://api.lyan.test/v1/reports",
                "query": [["page", "1"]],
                "data": {"visit_id": 5},
                "headers": [["User-Agent", "okhttp/4"], ["Cookie", "secret=1"], ["Authorization", "Bearer x"], ["Accept-Language", "fa"]],
            },
        },
        {
            "type": "breadcrumbs",
            "data": {
                "values": [
                    {"timestamp": f"2026-09-19T11:58:{n:02d}Z", "category": "db.sql.query", "level": "info", "message": f"select {n}", "data": {"executionTimeMs": n}}
                    for n in range(40)
                ]
            },
        },
    ],
}
EVENT_SUMMARY = {
    "eventID": EVENT["eventID"],
    "dateCreated": EVENT["dateCreated"],
    "message": "Undefined array key",
    "user": {"email": "rep@lyan.test"},
    "tags": EVENT["tags"],
}
TAGS = [
    {"key": "url", "totalValues": 321, "topValues": [{"value": "https://api.lyan.test/v1/reports", "count": 300}]},
    {"key": "release", "totalValues": 321, "topValues": [{"value": "2026.09.18", "count": 250}, {"value": "2026.09.10", "count": 71}]},
]

NEXT = '<{u}?cursor=0:0:1>; rel="previous"; results="false"; cursor="0:0:1", <{u}?cursor=0:25:0>; rel="next"; results="{more}"; cursor="0:25:0"'


def _issues(call):
    more = "false" if call["query"].get("cursor") else "true"
    return 200, [ISSUE, SECOND_ISSUE], {"Link": NEXT.format(u="http://x/api/0/organizations/lyan/issues/", more=more)}


def _update(call, org, gid):
    if gid not in ("4512", "4513"):
        return 404, {"detail": "The requested resource does not exist"}, {}
    base = copy.deepcopy(ISSUE if gid == "4512" else SECOND_ISSUE)
    body = call["json"]
    for key in ("status", "substatus", "priority", "isBookmarked", "hasSeen", "isSubscribed", "statusDetails"):
        if key in body:
            base[key] = body[key]
    if "assignedTo" in body:
        base["assignedTo"] = None if body["assignedTo"] is None else {"type": "user", "id": "8", "name": "Sara", "email": SARA["email"]}
    return base


def install_defaults(fake: FakeSentry) -> None:
    o = ORG
    fake.route("GET", r"/api/0/", {"version": "0", "auth": {"scopes": ["event:read", "event:write", "member:read", "org:read", "project:read", "team:read"]}, "user": ME})
    fake.route("GET", r"/api/0/organizations/", [{"id": "1", "slug": o, "name": "Lyan"}])
    fake.route("GET", rf"/api/0/organizations/{o}/projects/", PROJECTS)
    fake.route("GET", rf"/api/0/organizations/{o}/teams/", TEAMS)
    fake.route("GET", rf"/api/0/organizations/{o}/members/", lambda call: [m for m in MEMBERS if (call["query"].get("query") or [""])[0].lower() in json.dumps(m).lower()])
    fake.route("GET", rf"/api/0/organizations/{o}/issues/", _issues)
    fake.route("GET", rf"/api/0/organizations/{o}/shortids/(API-1A|api-1a)/", {"groupId": "4512", "shortId": "API-1A"})
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/", ISSUE)
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4513/", SECOND_ISSUE)
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/tags/", TAGS)
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/tags/url/", {"key": "url", "totalValues": 321, "uniqueValues": 2})
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/tags/url/values/", [{"value": "https://api.lyan.test/v1/reports", "count": 300, "lastSeen": "2026-09-19T11:59:00Z"}])
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/events/", [EVENT_SUMMARY])
    fake.route("GET", rf"/api/0/organizations/{o}/issues/4512/events/(latest|oldest|recommended|{EVENT['eventID']})/", EVENT)
    fake.route("GET", rf"/api/0/organizations/{o}/eventids/{EVENT['eventID']}/", {"projectSlug": "api", "groupId": "4512", "event": EVENT})
    fake.route("GET", rf"/api/0/projects/{o}/api/events/{EVENT['eventID']}/", EVENT)
    fake.route("GET", rf"/api/0/organizations/{o}/events/", {"data": [{"url": "https://api.lyan.test/v1/reports", "count()": 300}], "meta": {"fields": {"url": "string", "count()": "integer"}, "units": {"url": None, "count()": None}}})
    fake.route("GET", rf"/api/0/organizations/{o}/releases/", [{"version": "2026.09.18", "shortVersion": "2026.09.18", "dateCreated": "2026-09-18T09:00:00Z", "newGroups": 3, "projects": [{"slug": "api"}]}])
    fake.route("PUT", rf"/api/0/organizations/({o})/issues/(\d+)/", _update)
    fake.route(
        "POST",
        rf"/api/0/organizations/{o}/issues/4512/comments/",
        lambda call: (201, {"id": "901", "dateCreated": "2026-09-19T12:00:00Z", "data": {"text": call["json"]["text"]}}, {}),
    )


@contextmanager
def fake_sentry():
    with FakeSentry() as fake:
        install_defaults(fake)
        yield fake
