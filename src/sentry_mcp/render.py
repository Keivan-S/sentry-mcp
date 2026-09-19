"""Sentry payloads -> compact dicts a model reads well: crash point first, the app's own code with its source lines,
library noise collapsed, and every free-form value bounded in size."""

import json
from typing import Any

SOURCE_FRAMES = 5  # in-app frames that carry their source lines
LIBRARY_RUN = 3  # longer runs of library frames between app frames are collapsed
HIDDEN_HEADERS = {"cookie", "authorization", "x-csrf-token", "x-xsrf-token", "proxy-authorization"}
SURFACED_TAGS = ("level", "environment", "release", "transaction")


def clip(text: Any, limit: int = 1000) -> Any:
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars]"


def trim(value: Any, max_text: int = 300, max_items: int = 30, depth: int = 4) -> Any:
    """Bound an arbitrary JSON value (extra data, contexts, request bodies, frame vars) in size."""
    if isinstance(value, str):
        return clip(value, max_text)
    if isinstance(value, dict):
        if depth <= 0:
            return f"{{… {len(value)} keys}}"
        items = list(value.items())
        out = {str(k): trim(v, max_text, max_items, depth - 1) for k, v in items[:max_items]}
        if len(items) > max_items:
            out["…"] = f"{len(items) - max_items} more keys"
        return out
    if isinstance(value, list):
        if depth <= 0:
            return f"[… {len(value)} items]"
        out = [trim(v, max_text, max_items, depth - 1) for v in value[:max_items]]
        if len(value) > max_items:
            out.append(f"… {len(value) - max_items} more")
        return out
    return value


def compact(data: dict, keep: tuple[str, ...] = ()) -> dict:
    """Drop empty values, except the keys in `keep` (an empty result list is an answer, not noise)."""
    return {k: v for k, v in data.items() if k in keep or not (v is None or v == "" or v == [] or v == {})}


def _int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# --- people & org objects -----------------------------------------------------------------------------------------


def person(user: dict | None) -> dict | None:
    if not user:
        return None
    return compact({"id": user.get("id"), "name": user.get("name"), "email": user.get("email"), "username": user.get("username")}) or None


def actor(value: dict | None) -> str | None:
    """assignedTo / activity author -> "Name <email>" or "#team"."""
    if not value:
        return None
    if value.get("type") == "team":
        return f"#{value.get('slug') or value.get('name')}"
    name, email = value.get("name"), value.get("email")
    if name and email and name != email:
        return f"{name} <{email}>"
    return email or name or value.get("username") or (f"user:{value['id']}" if value.get("id") else None)


def organization(o: dict) -> dict:
    return compact({"slug": o.get("slug"), "name": o.get("name"), "id": o.get("id")})


def project(p: dict) -> dict:
    return compact(
        {
            "slug": p.get("slug"),
            "id": p.get("id"),
            "name": p.get("name") if p.get("name") != p.get("slug") else None,
            "platform": p.get("platform"),
            "teams": [t.get("slug") for t in p.get("teams") or [] if t.get("slug")],
            "is_member": p.get("isMember"),
        }
    )


def team(t: dict) -> dict:
    return compact({"slug": t.get("slug"), "id": t.get("id"), "name": t.get("name"), "members": t.get("memberCount"), "is_member": t.get("isMember")})


def member(m: dict) -> dict:
    user = m.get("user") or {}
    return compact(
        {
            "user_id": user.get("id"),
            "name": m.get("name") or user.get("name"),
            "email": m.get("email") or user.get("email"),
            "username": user.get("username") if user.get("username") not in (m.get("email"), user.get("email")) else None,
            "role": m.get("orgRole") or m.get("role"),
            "pending": m.get("pending") or None,
        }
    )


def release(r: dict) -> dict:
    deploy = r.get("lastDeploy") or {}
    return compact(
        {
            "version": r.get("version"),
            "short_version": r.get("shortVersion") if r.get("shortVersion") != r.get("version") else None,
            "created": r.get("dateCreated"),
            "released": r.get("dateReleased"),
            "first_event": r.get("firstEvent"),
            "last_event": r.get("lastEvent"),
            "new_issues": r.get("newGroups"),
            "projects": [p.get("slug") for p in r.get("projects") or []],
            "commits": r.get("commitCount") or None,
            "last_deploy": compact({"environment": deploy.get("environment"), "finished": deploy.get("dateFinished")}) or None,
        }
    )


# --- issues -------------------------------------------------------------------------------------------------------


def issue(g: dict) -> dict:
    meta = g.get("metadata") or {}
    return compact(
        {
            "id": g.get("id"),
            "short_id": g.get("shortId"),
            "title": g.get("title"),
            "culprit": g.get("culprit"),
            "project": (g.get("project") or {}).get("slug"),
            "level": g.get("level"),
            "status": g.get("status"),
            "substatus": g.get("substatus"),
            "priority": g.get("priority"),
            "events": _int(g.get("count")),
            "users": g.get("userCount"),
            "first_seen": g.get("firstSeen"),
            "last_seen": g.get("lastSeen"),
            "assigned_to": actor(g.get("assignedTo")),
            "file": meta.get("filename"),
            "function": meta.get("function"),
            "unhandled": g.get("isUnhandled") or None,
            "bookmarked": g.get("isBookmarked") or None,
            "comments": g.get("numComments") or None,
            "issue_type": g.get("issueType") if g.get("issueType") not in (None, "error") else None,
            "permalink": g.get("permalink"),
        }
    )


def _activity(a: dict) -> dict:
    data = a.get("data") or {}
    return compact(
        {
            "type": a.get("type"),
            "date": a.get("dateCreated"),
            "by": actor(a.get("user")) or "Sentry",
            "text": clip(data.get("text"), 1000) if a.get("type") == "note" else None,
            "data": trim(data, 200, 10, 2) if a.get("type") != "note" else None,
        }
    )


def issue_detail(g: dict) -> dict:
    out = issue(g)
    out.update(
        compact(
            {
                "first_release": (g.get("firstRelease") or {}).get("version"),
                "last_release": (g.get("lastRelease") or {}).get("version"),
                "status_details": trim(g.get("statusDetails"), 200, 10, 2),
                "seen_by_you": g.get("hasSeen"),
                "subscribed": g.get("isSubscribed"),
                "user_reports": g.get("userReportCount") or None,
                "participants": [actor(p) for p in g.get("participants") or []],
                # newest first; "first_seen" and friends are noise next to comments and status changes
                "activity": [_activity(a) for a in (g.get("activity") or [])[:15]],
            }
        )
    )
    return out


def tag_summary(tags: list[dict]) -> list[dict]:
    return [
        compact(
            {
                "key": t.get("key"),
                "events": t.get("totalValues"),
                "top": [f"{clip(str(v.get('value')), 120)} ({v.get('count')})" for v in t.get("topValues") or []],
            }
        )
        for t in tags or []
    ]


def tag_value(v: dict) -> dict:
    return compact({"value": clip(v.get("value"), 500), "count": v.get("count"), "first_seen": v.get("firstSeen"), "last_seen": v.get("lastSeen")})


# --- events -------------------------------------------------------------------------------------------------------


def _where(frame: dict) -> str:
    place = frame.get("filename") or frame.get("absPath") or frame.get("module") or "?"
    if frame.get("lineNo"):
        place = f"{place}:{frame['lineNo']}"
    return f"{place} in {frame['function']}" if frame.get("function") else place


def _source(context: list, line_no: Any) -> str:
    lines = []
    for item in context:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        number, code = item
        marker = "→" if number == line_no else " "
        lines.append(f"{marker}{number:>5} | {clip(str(code or '').rstrip(), 200)}")
    return "\n".join(lines)


def frames(raw: list[dict] | None, max_frames: int = 40) -> list:
    """Crash point first. App frames are dicts (the first few with source and vars); library frames are one-line strings,
    and long runs of them between app frames are collapsed."""
    ordered = list(reversed(raw or []))
    has_app = any(f.get("inApp") for f in ordered)
    out: list = []
    sourced = 0
    i = 0
    while i < len(ordered):
        frame = ordered[i]
        is_app = bool(frame.get("inApp")) or not has_app  # no app frames at all: treat everything as relevant
        if not is_app and i > 0:
            j = i
            while j < len(ordered) and not ordered[j].get("inApp"):
                j += 1
            run = ordered[i:j]
            if len(run) > LIBRARY_RUN:
                out += [_where(run[0]), f"… {len(run) - 2} library frames", _where(run[-1])]
                i = j
                continue
        if is_app:
            item: dict = {"at": _where(frame)}
            if frame.get("inApp"):
                item["app"] = True
            if sourced < SOURCE_FRAMES and frame.get("context"):
                item["source"] = _source(frame["context"], frame.get("lineNo"))
                if frame.get("vars"):
                    item["vars"] = trim(frame["vars"], 200, 20, 3)
                sourced += 1
            out.append(item)
        else:
            out.append(_where(frame))
        i += 1
    if len(out) > max_frames:
        out = out[:max_frames] + [f"… {len(out) - max_frames} more frames"]
    return out


def _exceptions(data: dict | None, max_frames: int) -> list[dict]:
    # Sentry lists a chain oldest-first: the last value is the exception that surfaced. Show that one first.
    out = []
    for exc in reversed((data or {}).get("values") or []):
        mechanism = exc.get("mechanism") or {}
        out.append(
            compact(
                {
                    "type": exc.get("type"),
                    "value": clip(exc.get("value"), 2000),
                    "module": exc.get("module"),
                    "handled": mechanism.get("handled"),
                    "mechanism": mechanism.get("type"),
                    "frames": frames((exc.get("stacktrace") or {}).get("frames"), max_frames),
                }
            )
        )
    return out


def _thread_frames(data: dict | None, max_frames: int) -> list:
    threads = (data or {}).get("values") or []
    crashed = next((t for t in threads if t.get("crashed")), None) or next((t for t in threads if t.get("current")), None)
    return frames(((crashed or {}).get("stacktrace") or {}).get("frames"), max_frames) if crashed else []


def _pairs(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return {p[0]: p[1] for p in value or [] if isinstance(p, (list, tuple)) and len(p) == 2}


def _request(data: dict | None) -> dict | None:
    if not data:
        return None
    headers = {k: clip(str(v), 200) for k, v in _pairs(data.get("headers")).items() if str(k).lower() not in HIDDEN_HEADERS}
    query = data.get("query")
    if isinstance(query, list):
        query = "&".join(f"{k}={v}" for k, v in _pairs(query).items())
    return compact(
        {
            "method": data.get("method"),
            "url": data.get("url"),
            "query": clip(query, 1000),
            "body": trim(data.get("data"), 500, 30, 3),
            "headers": headers,
        }
    ) or None


def _breadcrumbs(data: dict | None, limit: int) -> list[str]:
    values = (data or {}).get("values") or []
    if limit <= 0:
        return []
    out = [f"… {len(values) - limit} earlier breadcrumbs"] if len(values) > limit else []
    for crumb in values[-limit:]:
        level = crumb.get("level")
        parts = [
            crumb.get("timestamp"),
            crumb.get("category") or crumb.get("type"),
            f"[{level}]" if level and level not in ("info", "debug") else None,
            clip(crumb.get("message"), 400),
        ]
        line = " ".join(str(p) for p in parts if p)
        if crumb.get("data"):
            line += " " + clip(json.dumps(crumb["data"], ensure_ascii=False, default=str), 300)
        out.append(line)
    return out


def _contexts(contexts: dict | None) -> dict:
    out = {}
    for name, value in (contexts or {}).items():
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "type"}
        out[name] = trim(value, 200, 20, 3)
    return out


def _message(ev: dict, entries: dict) -> str | None:
    message = entries.get("message") or {}
    return message.get("formatted") or message.get("message") or ev.get("message") or None


def _user(user: dict | None) -> dict | None:
    if not user:
        return None
    return compact(
        {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username"),
            "name": user.get("name"),
            "ip": user.get("ip_address"),
            "data": trim(user.get("data"), 200, 15, 2),
        }
    ) or None


def event(ev: dict, max_frames: int = 40, breadcrumbs: int = 30) -> dict:
    tags = {t.get("key"): t.get("value") for t in ev.get("tags") or [] if isinstance(t, dict)}
    entries = {e.get("type"): e.get("data") or {} for e in ev.get("entries") or [] if isinstance(e, dict)}
    rel = ev.get("release")
    exceptions = _exceptions(entries.get("exception"), max_frames)
    stack = [] if exceptions else frames((entries.get("stacktrace") or {}).get("frames"), max_frames) or _thread_frames(entries.get("threads"), max_frames)
    sdk = ev.get("sdk") or {}
    return compact(
        {
            "event_id": ev.get("eventID") or ev.get("id"),
            "issue_id": ev.get("groupID"),
            "date": ev.get("dateCreated"),
            "title": ev.get("title"),
            "message": clip(_message(ev, entries), 2000),
            "level": tags.get("level"),
            "location": ev.get("location") or ev.get("culprit"),
            "environment": tags.get("environment"),
            "release": (rel.get("version") if isinstance(rel, dict) else rel) or tags.get("release"),
            "transaction": tags.get("transaction"),
            "user": _user(ev.get("user")),
            "exceptions": exceptions,
            "stacktrace": stack,
            "request": _request(entries.get("request")),
            "breadcrumbs": _breadcrumbs(entries.get("breadcrumbs"), breadcrumbs),
            "tags": {k: clip(str(v), 300) for k, v in tags.items() if k not in SURFACED_TAGS},
            "contexts": _contexts(ev.get("contexts")),
            "extra": trim(ev.get("context"), 500, 30, 3),
            "platform": ev.get("platform"),
            "sdk": " ".join(str(x) for x in (sdk.get("name"), sdk.get("version")) if x),
        }
    )


def event_summary(e: dict) -> dict:
    tags = {t.get("key"): t.get("value") for t in e.get("tags") or [] if isinstance(t, dict)}
    user = e.get("user") or {}
    return compact(
        {
            "event_id": e.get("eventID") or e.get("id"),
            "date": e.get("dateCreated"),
            "message": clip(e.get("message") or e.get("title"), 300),
            "release": tags.get("release"),
            "environment": tags.get("environment"),
            "user": user.get("email") or user.get("username") or user.get("id") or user.get("ip_address") or tags.get("user"),
            "url": tags.get("url"),
            "transaction": tags.get("transaction"),
            "server": tags.get("server_name"),
        }
    )
