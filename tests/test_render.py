from fake_sentry import EVENT, ISSUE

from sentry_mcp import render


def test_crash_point_first_with_app_source_and_collapsed_library_runs():
    frames = render.event(EVENT)["exceptions"][0]["frames"]
    assert frames[0] == "/vendor/laravel/framework/src/HandleExceptions.php:255 in handleError"
    service = frames[1]
    assert service["at"] == "/app/Services/ReportService.php:88 in store" and service["app"] is True
    assert "→   88 |     $brand = $data['brand_id'];" in service["source"]
    assert service["vars"] == {"$data": {"visit_id": 5}}
    assert frames[2]["at"].startswith("/app/Http/Controllers/ReportController.php:31")
    assert frames[3:6] == [
        "/vendor/laravel/framework/src/Pipeline.php:107 in handle",
        "… 6 library frames",
        "/vendor/laravel/framework/src/Pipeline.php:100 in handle",
    ]
    assert frames[6]["at"] == "/public/index.php:52 in {main}"


def test_surfaced_exception_comes_first():
    exceptions = render.event(EVENT)["exceptions"]
    assert [e["type"] for e in exceptions] == ["ErrorException", "PDOException"]
    assert exceptions[0]["handled"] is False


def test_event_surfaces_request_user_and_tags_without_secrets():
    ev = render.event(EVENT)
    assert ev["request"]["query"] == "page=1"
    assert ev["request"]["headers"] == {"User-Agent": "okhttp/4", "Accept-Language": "fa"}
    assert ev["environment"] == "production" and ev["release"] == "2026.09.18"
    assert ev["tags"] == {"url": "https://api.lyan.test/v1/reports", "server_name": "api-1"}
    assert ev["user"]["ip"] == "10.0.0.5"
    assert ev["contexts"]["runtime"] == {"name": "php", "version": "8.4.3"}
    assert ev["sdk"] == "sentry.php.laravel 4.26.0"


def test_breadcrumbs_keep_the_latest():
    crumbs = render.event(EVENT, breadcrumbs=5)["breadcrumbs"]
    assert crumbs[0] == "… 35 earlier breadcrumbs"
    assert crumbs[-1] == '2026-09-19T11:58:39Z db.sql.query select 39 {"executionTimeMs": 39}'
    assert "breadcrumbs" not in render.event(EVENT, breadcrumbs=0)


def test_frame_cap():
    frames = [{"filename": f"f{n}.php", "lineNo": n, "inApp": True} for n in range(60)]
    out = render.frames(frames, max_frames=10)
    assert len(out) == 11 and out[-1] == "… 50 more frames"


def test_all_library_frames_are_kept_when_nothing_is_in_app():
    frames = [{"filename": f"lib{n}.js", "lineNo": n, "context": [[n, "x"]]} for n in range(6)]
    out = render.frames(frames)
    assert len(out) == 6 and all(isinstance(f, dict) for f in out)
    assert sum("source" in f for f in out) == render.SOURCE_FRAMES


def test_issue_summary():
    issue = render.issue(ISSUE)
    assert issue["events"] == 321
    assert issue["project"] == "api"
    assert "issue_type" not in issue and "assigned_to" not in issue and "bookmarked" not in issue


def test_actor():
    assert render.actor({"type": "team", "name": "backend"}) == "#backend"
    assert render.actor({"type": "user", "name": "Sara", "email": "s@x"}) == "Sara <s@x>"
    assert render.actor(None) is None


def test_trim_bounds_size():
    value = render.trim({"a": "x" * 1000, "b": list(range(100)), "c": {"d": {"e": {"f": 1}}}}, 10, 5, 3)
    assert value["a"].startswith("xxxxxxxxxx…")
    assert value["b"][-1] == "… 95 more"
    assert value["c"] == {"d": {"e": "{… 1 keys}"}}
