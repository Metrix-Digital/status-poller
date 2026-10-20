"""Railway adapter + page migration guard. requests is stubbed; no network."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import poll  # noqa: E402

RAILWAY = next(v for v in poll.VENDORS if v["name"] == "Railway")
NOW = datetime(2026, 10, 20, 18, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "", bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._bad_json = bad_json

    def json(self) -> object:
        if self._bad_json:
            raise ValueError("Expecting value")
        return self._payload


def incident(title: str, status: str, *impacts: str) -> dict:
    return {
        "id": title,
        "title": title,
        "status": status,
        "components": [{"name": "Builds", "groupName": "Core", "impact": i} for i in impacts],
        "updates": [{"status": status, "message": "..."}],
    }


def railway_body(*incidents: dict) -> dict:
    return {"entries": [], "activeIncidents": list(incidents), "recentIncidents": [], "maintenances": [], "generatedAt": "x"}


def stub_get(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> list[str]:
    calls: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> FakeResponse:
        calls.append(url)
        return response

    monkeypatch.setattr(poll.requests, "get", fake_get)
    return calls


def test_railway_no_incidents_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_get(monkeypatch, FakeResponse(payload=railway_body()))
    assert poll.fetch_status(RAILWAY) == ("none", "All Systems Operational")


def test_railway_major_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    body = railway_body(incident("New builds are temporarily paused", "IDENTIFIED", "MAJOR_OUTAGE"))
    stub_get(monkeypatch, FakeResponse(payload=body))
    indicator, desc = poll.fetch_status(RAILWAY)
    assert indicator == "major"
    assert desc == "New builds are temporarily paused (IDENTIFIED)"


def test_railway_worst_impact_wins_across_incidents(monkeypatch: pytest.MonkeyPatch) -> None:
    body = railway_body(
        incident("Slow deploys", "INVESTIGATING", "DEGRADED_PERFORMANCE", "PARTIAL_OUTAGE"),
        incident("Network down", "IDENTIFIED", "FULL_OUTAGE"),
        incident("Builds paused", "MONITORING", "MAJOR_OUTAGE"),
    )
    stub_get(monkeypatch, FakeResponse(payload=body))
    indicator, desc = poll.fetch_status(RAILWAY)
    assert indicator == "critical"
    assert desc == "Slow deploys (INVESTIGATING); Network down (IDENTIFIED); Builds paused (MONITORING)"


def test_railway_unknown_impact_is_minor_and_logged(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    stub_get(monkeypatch, FakeResponse(payload=railway_body(incident("Odd", "IDENTIFIED", "SOMETHING_NEW"))))
    assert poll.fetch_status(RAILWAY)[0] == "minor"
    assert "SOMETHING_NEW" in capsys.readouterr().err


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(bad_json=True),
        FakeResponse(payload={"unexpected": True}),
        FakeResponse(payload={"activeIncidents": "not-a-list-of-dicts"}),
        FakeResponse(payload=["not", "a", "dict"]),
        FakeResponse(payload={"activeIncidents": None}),
        FakeResponse(payload={"activeIncidents": {}}),
    ],
)
def test_railway_malformed_is_unknown(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> None:
    stub_get(monkeypatch, response)
    assert poll.fetch_status(RAILWAY)[0] == "unknown"


def capture_slack(monkeypatch: pytest.MonkeyPatch, delivered: bool = True) -> list[str]:
    posts: list[str] = []

    def fake_post(_url: str, text: str) -> bool:
        posts.append(text)
        return delivered

    monkeypatch.setattr(poll, "post_slack", fake_post)
    return posts


def atlassian_count() -> int:
    return sum(1 for v in poll.VENDORS if v.get("kind", "atlassian") == "atlassian")


def test_guard_quiet_when_statuspage_present(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_get(monkeypatch, FakeResponse(text="<script src='cdn.statuspage.io/x.js'>"))
    posts = capture_slack(monkeypatch)
    state: dict = {}
    poll.check_page_migrations(state, "hook", NOW)
    assert posts == []
    assert len(calls) == atlassian_count()
    assert RAILWAY["page"] not in calls  # the railway-kind vendor is not guarded
    assert state[poll.PAGE_GUARD_KEY] == {"last_check": NOW.isoformat(), "warned": []}


def test_guard_warns_once_per_vendor_and_does_not_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_get(monkeypatch, FakeResponse(text="<html>our own status site</html>"))
    posts = capture_slack(monkeypatch)
    state: dict = {}
    poll.check_page_migrations(state, "hook", NOW)
    assert len(posts) == atlassian_count()
    assert any(p.startswith("OpenAI status page no longer looks like Atlassian Statuspage") for p in posts)

    # Same day: not re-checked at all.
    poll.check_page_migrations(state, "hook", NOW + timedelta(hours=1))
    # Next day: re-checked, still not Atlassian, but already warned.
    poll.check_page_migrations(state, "hook", NOW + timedelta(days=1, minutes=1))
    assert len(posts) == atlassian_count()


def test_guard_rearms_after_page_changes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    posts = capture_slack(monkeypatch)
    state: dict = {}
    stub_get(monkeypatch, FakeResponse(text="<html>no marker</html>"))
    poll.check_page_migrations(state, "hook", NOW)
    stub_get(monkeypatch, FakeResponse(text="Powered by Atlassian Statuspage"))
    poll.check_page_migrations(state, "hook", NOW + timedelta(days=1, minutes=1))
    assert state[poll.PAGE_GUARD_KEY]["warned"] == []
    stub_get(monkeypatch, FakeResponse(text="<html>no marker</html>"))
    poll.check_page_migrations(state, "hook", NOW + timedelta(days=2, minutes=2))
    assert len(posts) == 2 * atlassian_count()


def test_guard_failed_slack_post_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_get(monkeypatch, FakeResponse(text="<html>no marker</html>"))
    posts = capture_slack(monkeypatch, delivered=False)
    state: dict = {}
    poll.check_page_migrations(state, "hook", NOW)
    assert state[poll.PAGE_GUARD_KEY]["warned"] == []
    poll.check_page_migrations(state, "hook", NOW + timedelta(days=1, minutes=1))
    assert len(posts) == 2 * atlassian_count()


def test_guard_fetch_failure_does_not_warn(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    stub_get(monkeypatch, FakeResponse(status_code=503))
    posts = capture_slack(monkeypatch)
    state: dict = {}
    poll.check_page_migrations(state, "hook", NOW)
    assert posts == []
    assert "page guard: HTTP 503" in capsys.readouterr().err
