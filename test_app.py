"""Tests for Streetwise, through LocalRunner, with the network stubbed."""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import LocalRunner, RateLimitedError, ServiceUnavailableError

from app import App, NeedAPlace, NeedTwoPlaces, PlaceNotFound, WalkTooLong, sun_times

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

PLACES = {
    "old street": ("51.5256", "-0.0875", "Old Street, London"),
    "angel": ("51.5322", "-0.1058", "Angel, London"),
    "brixton": ("51.4613", "-0.1156", "Brixton, London"),
    "manchester": ("53.4808", "-2.2426", "Manchester, England"),
}
CRIMES = [
    {"category": "violent-crime"}, {"category": "violent-crime"},
    {"category": "robbery"}, {"category": "theft-from-the-person"},
    {"category": "anti-social-behaviour"},
]


def fake_fetch(crimes=CRIMES):
    def fetch(url, timeout=6.0, headers=None):
        if "crime-last-updated" in url:
            return b'{"date": "2026-09-01"}'
        if "crimes-street" in url:
            return json.dumps(crimes).encode()
        if "postcodes.io" in url:
            return json.dumps({"result": {"latitude": 51.546, "longitude": -0.075, "postcode": "E8 3PH"}}).encode()
        if "nominatim" in url:
            for key, (lat, lon, name) in PLACES.items():
                if key.replace(" ", "+") in url.lower():
                    return json.dumps([{"lat": lat, "lon": lon, "display_name": name}]).encode()
            return b"[]"
        raise AssertionError(url)
    return fetch


def make(fetch=None):
    app = App()
    app._fetch = fetch or fake_fetch()
    app._pause = lambda: None
    return app


def call(inputs, outcome="walk.check", *, app=None, explicit=False):
    return LocalRunner(app or make()).call(
        outcome, inputs, granted_secrets={}, now=NOW, request_id="t1", explicit=explicit,
    ).result


def test_the_manifest_is_valid():
    assert [o.id for o in App().manifest.outcomes] == ["walk.check", "area.check"]


def test_a_walk_is_counted():
    r = call({"from_place": "Old Street", "to_place": "Angel", "time": "23:30"})
    assert r.data["crimes_total"] == 5
    assert r.data["violence_and_robbery"] == 3
    assert r.data["theft_from_person"] == 1
    assert r.data["month_used"] == "2026-09"
    assert r.data["dark"] is True
    assert r.data["walk_time"] == "23:30"
    assert 10 <= r.data["walk_minutes"] <= 30
    assert "violence and sexual offences 2" in r.data["top_categories"]
    assert "safe" not in r.narrative.lower()


def test_midday_is_light():
    r = call({"from_place": "Old Street", "to_place": "Angel", "time": "12:00"})
    assert r.data["dark"] is False


def test_no_time_uses_now():
    r = call({"from_place": "Old Street", "to_place": "Angel", "time": ""})
    assert r.data["walk_time"] == "15:00"


def test_no_crimes_recorded():
    r = call({"from_place": "Old Street", "to_place": "Angel"}, app=make(fake_fetch([])))
    assert r.data["crimes_total"] == 0
    assert r.data["top_categories"] == "none recorded"


def test_an_area_is_counted_from_a_postcode():
    r = call({"place": "E8 3PH"}, "area.check")
    assert r.data["place_used"] == "E8 3PH"
    assert r.data["crimes_total"] == 5
    assert r.data["radius_m"] == 400


def test_an_area_from_a_name():
    r = call({"place": "Brixton"}, "area.check")
    assert r.data["place_used"] == "Brixton, London"


def test_missing_places_say_what_to_type():
    with pytest.raises(NeedTwoPlaces):
        call({"from_place": "Old Street", "to_place": ""})
    with pytest.raises(NeedAPlace):
        call({"place": ""}, "area.check")


def test_unknown_place_is_named():
    with pytest.raises(PlaceNotFound) as failure:
        call({"place": "Narnia"}, "area.check")
    assert "Narnia" in failure.value.user_message


def test_walk_too_long():
    with pytest.raises(WalkTooLong):
        call({"from_place": "Old Street", "to_place": "Manchester"})


def test_rate_limit_and_timeout():
    def limited(url, timeout=6.0, headers=None):
        raise urllib.error.HTTPError(url, 429, "busy", {}, None)

    def slow(url, timeout=6.0, headers=None):
        raise TimeoutError("slow")

    with pytest.raises(RateLimitedError):
        call({"place": "Brixton"}, "area.check", app=make(limited))
    with pytest.raises(ServiceUnavailableError):
        call({"place": "Brixton"}, "area.check", app=make(slow))


def test_picked_by_name_answers_as_usual():
    picked = call({"place": "Brixton"}, "area.check", explicit=True)
    assert picked.data == call({"place": "Brixton"}, "area.check").data


def test_sunset_in_november_london():
    sunrise, sunset = sun_times(51.4988, -0.1749, NOW.date())
    assert sunset.startswith("16:0")
    assert sunrise.startswith("07:2")
