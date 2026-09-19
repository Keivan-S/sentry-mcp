import pytest
from fake_sentry import EVENT, ORG

from sentry_mcp.client import SentryError, next_cursor, status_body, time_range
from sentry_mcp.config import ConfigError, Settings, normalize_url, resolve_token


def test_org_is_the_only_one_the_token_sees(client):
    assert client.org() == ORG
    assert client.org("other") == "other"


def test_several_orgs_need_an_explicit_choice(client, sentry):
    sentry.route("GET", r"/api/0/organizations/", [{"slug": "a"}, {"slug": "b"}])
    with pytest.raises(ValueError, match=r"several organizations \(a, b\)"):
        client.org()


def test_search_issues_defaults_to_unresolved_in_every_accessible_project(client, sentry):
    result = client.search_issues()
    call = sentry.last("GET", "/issues/")
    assert call["query"]["query"] == ["is:unresolved"]
    assert call["query"]["project"] == ["-1"]
    assert call["query"]["statsPeriod"] == ["14d"]
    assert [i["short_id"] for i in result["issues"]] == ["API-1A", "WEB-2"]
    assert result["issues"][0]["events"] == 321
    assert result["next_cursor"] == "0:25:0"


def test_empty_query_is_sent_so_sentry_does_not_fall_back_to_unresolved(client, sentry):
    client.search_issues(query="", projects=["api", "12"], environments=["production"], start="2026-09-01T00:00:00", end="2026-09-02T00:00:00", cursor="0:25:0")
    call = sentry.last("GET", "/issues/")
    assert call["query"]["query"] == [""]
    assert call["query"]["project"] == ["11", "12"]
    assert call["query"]["environment"] == ["production"]
    assert call["query"]["start"] == ["2026-09-01T00:00:00"]
    assert "statsPeriod" not in call["query"]


def test_unknown_project_lists_the_real_ones(client):
    with pytest.raises(ValueError, match="Projects: api, web"):
        client.search_issues(projects=["nope"])


@pytest.mark.parametrize("ref", ["4512", "API-1A", "https://sentry.lyan.test/organizations/lyan/issues/4512/?project=11"])
def test_issue_references(client, ref):
    assert client.issue_id(ORG, ref) == "4512"


def test_bad_issue_reference_is_explained(client):
    with pytest.raises(ValueError, match="not an issue id"):
        client.issue_id(ORG, "what?")


def test_get_issue_bundles_detail_tags_and_the_latest_event(client):
    result = client.get_issue("API-1A")
    assert result["issue"]["first_release"] == "2026.09.10"
    assert result["issue"]["activity"][0]["text"] == "Looking into it"
    assert result["tags"][1]["top"] == ["2026.09.18 (250)", "2026.09.10 (71)"]
    top = result["event"]["exceptions"][0]
    assert top["type"] == "ErrorException"
    assert result["event"]["exceptions"][1]["type"] == "PDOException"


def test_get_event_without_project_looks_it_up(client, sentry):
    result = client.get_event(EVENT["eventID"][:8] + "-" + EVENT["eventID"][8:])
    assert result["project"] == "api"
    assert result["event"]["event_id"] == EVENT["eventID"]
    assert sentry.last("GET", "/eventids/")


def test_tag_values(client, sentry):
    result = client.issue_tag_values("4512", "url")
    assert sentry.last("GET", "/values/")["query"]["sort"] == ["count"]
    assert result["events_with_tag"] == 321
    assert result["values"][0]["count"] == 300


def test_search_events_passes_fields_and_drops_empty_units(client, sentry):
    result = client.search_events(["url", "count()"], query="level:error", sort="-count()")
    call = sentry.last("GET", "/events/")
    assert call["query"]["field"] == ["url", "count()"]
    assert call["query"]["sort"] == ["-count()"]
    assert call["query"]["dataset"] == ["errors"]
    assert result["rows"][0]["count()"] == 300
    assert "units" not in result


@pytest.mark.parametrize(
    ("status", "options", "expected"),
    [
        ("resolved", {}, {"status": "resolved", "statusDetails": {}}),
        ("resolved_in_next_release", {}, {"status": "resolved", "statusDetails": {"inNextRelease": True}}),
        ("archived", {}, {"status": "ignored", "substatus": "archived_until_escalating", "statusDetails": {}}),
        ("archived", {"archive_forever": True}, {"status": "ignored", "substatus": "archived_forever", "statusDetails": {}}),
        ("archived", {"archive_minutes": 60}, {"status": "ignored", "substatus": "archived_until_condition_met", "statusDetails": {"ignoreDuration": 60}}),
    ],
)
def test_status_bodies(status, options, expected):
    args = {"archive_minutes": None, "archive_until_count": None, "archive_forever": False, **options}
    assert status_body(status, **args) == expected


def test_archive_options_need_archived_status():
    with pytest.raises(ValueError, match="only go with status='archived'"):
        status_body("resolved", 10, None, False)
    with pytest.raises(ValueError, match="Pick one"):
        status_body("archived", 10, 5, False)


@pytest.mark.parametrize(
    ("who", "actor"),
    [("me", "user:7"), ("sara@lyan.test", "user:8"), ("#backend", "team:3"), ("team:backend", "team:3"), ("user:99", "user:99"), ("none", None)],
)
def test_assignees(client, sentry, who, actor):
    client.update_issues(["4512"], assign_to=who)
    assert sentry.last("PUT", "/issues/4512/")["json"] == {"assignedTo": actor}


def test_ambiguous_assignee_is_refused(client):
    with pytest.raises(ValueError, match="several members"):
        client.update_issues(["4512"], assign_to="lyan.test")


def test_update_reports_per_issue_failures(client):
    result = client.update_issues(["API-1A", "999"], status="resolved", priority="low", bookmark=True)
    assert [i["id"] for i in result["updated"]] == ["4512"]
    assert result["updated"][0]["status"] == "resolved"
    assert result["updated"][0]["bookmarked"] is True
    assert result["failed"][0]["issue"] == "999"


def test_update_with_nothing_to_change_is_refused(client):
    with pytest.raises(ValueError, match="Nothing to change"):
        client.update_issues(["4512"])


def test_update_that_fails_everywhere_raises(client):
    with pytest.raises(SentryError) as caught:
        client.update_issues(["999"], status="resolved")
    assert caught.value.status == 404


def test_comment(client, sentry):
    result = client.add_comment("API-1A", "Fixed in 2026.09.19")
    assert result == {"issue_id": "4512", "comment_id": "901", "date": "2026-09-19T12:00:00Z", "text": "Fixed in 2026.09.19"}
    assert sentry.last("POST", "/comments/")["json"] == {"text": "Fixed in 2026.09.19"}


def test_wrong_token_is_a_401(client, sentry):
    sentry.token = "something-else"
    with pytest.raises(SentryError) as caught:
        client.search_issues()
    assert caught.value.status == 401


def test_redirects_are_not_followed(client, sentry):
    sentry.route("GET", r"/api/0/organizations/lyan/issues/", (301, {}, {"Location": "https://sentry.lyan.test/api/0/organizations/lyan/issues/"}))
    with pytest.raises(SentryError) as caught:
        client.search_issues()
    assert caught.value.location.startswith("https://sentry.lyan.test/")


def test_info(client):
    info = client.info()
    assert info["user"]["email"] == "keivan@lyan.test"
    assert info["default_org"] == ORG
    assert "event:write" in info["scopes"]


def test_next_cursor_needs_results_true():
    assert next_cursor('<x>; rel="next"; results="false"; cursor="0:25:0"') is None
    assert next_cursor(None) is None


def test_time_range():
    assert time_range("24h", None, None) == {"statsPeriod": "24h"}
    assert time_range(None, None, None) == {}
    assert time_range("24h", "2026-09-01", None)["start"] == "2026-09-01"
    with pytest.raises(ValueError, match="period"):
        time_range("yesterday", None, None)


@pytest.mark.parametrize(
    ("raw", "url"),
    [
        ("https://sntry.lyandigital.net/", "https://sntry.lyandigital.net"),
        ("https://sntry.lyandigital.net/api/0/", "https://sntry.lyandigital.net"),
        ("https://sntry.lyandigital.net/organizations/lyan/issues/4512/", "https://sntry.lyandigital.net"),
        ("HTTPS://tools.local/sentry/", "https://tools.local/sentry"),
    ],
)
def test_normalize_url(raw, url):
    assert normalize_url(raw) == url


def test_url_must_be_absolute():
    with pytest.raises(ConfigError, match="full address"):
        normalize_url("sntry.lyandigital.net")


def test_token_falls_back_to_the_credential_store(env, monkeypatch):
    import keyring

    monkeypatch.delenv("SENTRY_AUTH_TOKEN")
    settings = Settings.from_env()
    monkeypatch.setattr(keyring, "get_password", lambda service, account: "stored" if (service, account) == ("sentry-mcp", settings.host) else None)
    assert resolve_token(settings) == "stored"
    monkeypatch.setattr(keyring, "get_password", lambda service, account: None)
    with pytest.raises(ConfigError, match="sentry-mcp set-token"):
        resolve_token(settings)
