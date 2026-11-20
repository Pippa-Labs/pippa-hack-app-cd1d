"""Walk Home Check: recorded street crime along a walk, and whether it is dark.

Facts only. It never calls a walk "safe": it counts what police recorded in a
named month, and works out sunset offline.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
USER_AGENT = "pippa-agent-app-walkhomecheck/0.1 (https://github.com/Pippa-Labs)"
LONDON = ZoneInfo("Europe/London")
POSTCODE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?(\s*\d[A-Za-z]{2})?$")
MAX_WALK_KM = 6.0
CORRIDOR_M = 120
AREA_M = 400

LABELS = {
    "anti-social-behaviour": "anti-social behaviour",
    "bicycle-theft": "bicycle theft",
    "burglary": "burglary",
    "criminal-damage-arson": "criminal damage and arson",
    "drugs": "drugs",
    "other-theft": "other theft",
    "possession-of-weapons": "possession of weapons",
    "public-order": "public order",
    "robbery": "robbery",
    "shoplifting": "shoplifting",
    "theft-from-the-person": "theft from the person",
    "vehicle-crime": "vehicle crime",
    "violent-crime": "violence and sexual offences",
    "other-crime": "other crime",
}
VIOLENT = {"violent-crime", "robbery"}


class PlaceNotFound(AgentAppError):
    user_message = "I could not find that place."
    retryable = False


class NeedTwoPlaces(AgentAppError):
    user_message = "Tell me where the walk starts and ends, for example: from Old Street to Angel."
    retryable = False


class NeedAPlace(AgentAppError):
    user_message = "Tell me which place to check, for example a station, street or postcode."
    retryable = False


class WalkTooLong(AgentAppError):
    user_message = "That walk is over 6 km, too long for me to check. Try a shorter stretch."
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── network seam ──────────────────────────────────────────────────────
    def _fetch(self, url: str, timeout: float = 6.0, headers: dict | None = None) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _pause(self) -> None:
        time.sleep(1.0)  # Nominatim allows one request a second

    def _get_json(self, url: str, what: str, timeout: float = 6.0, headers: dict | None = None):
        try:
            raw = self._fetch(url, timeout, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 429:
                raise RateLimitedError(f"{what} rate limited")
            raise ServiceUnavailableError(f"{what} returned {exc.code}")
        except TimeoutError as exc:
            raise ServiceUnavailableError(f"{what} timed out: {exc}")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"could not reach {what}: {exc.reason}")
        try:
            return json.loads(raw)
        except ValueError:
            raise ServiceUnavailableError(f"{what} sent something that is not JSON")

    # ── geocoding ─────────────────────────────────────────────────────────
    def _geocode(self, place: str, first: bool = True) -> tuple[float, float, str]:
        text = " ".join(place.split())
        if POSTCODE.match(text):
            kind = "postcodes" if " " in text or len(text) > 4 else "outcodes"
            data = self._get_json(
                f"https://api.postcodes.io/{kind}/{quote(text, safe='')}", "postcodes.io", 4.0)
            res = (data or {}).get("result")
            if res and res.get("latitude") is not None:
                return float(res["latitude"]), float(res["longitude"]), res.get("postcode") or res.get("outcode") or text.upper()
        if not first:
            self._pause()
        for extra in ({"viewbox": "-0.52,51.70,0.34,51.28", "bounded": "1"}, {}):
            query = {"q": text, "format": "json", "limit": "1", "countrycodes": "gb", **extra}
            data = self._get_json("https://nominatim.openstreetmap.org/search?" + urlencode(query), "Nominatim", 5.0)
            if data:
                hit = data[0]
                name = ", ".join(hit.get("display_name", text).split(", ")[:2])
                return float(hit["lat"]), float(hit["lon"]), name
            if not extra:
                break
            self._pause()
        error = PlaceNotFound(f"no coordinates for {text!r}")
        error.user_message = f"I could not find {text}. Try a station, street or postcode."
        raise error

    # ── police data ───────────────────────────────────────────────────────
    def _month(self) -> str:
        data = self._get_json("https://data.police.uk/api/crime-last-updated", "police.uk", 4.0)
        return str((data or {}).get("date", ""))[:7]

    def _crimes(self, points: list[tuple[float, float]], month: str) -> list[dict]:
        poly = ":".join(f"{lat:.5f},{lon:.5f}" for lat, lon in points)
        query = {"poly": poly}
        if month:
            query["date"] = month
        data = self._get_json("https://data.police.uk/api/crimes-street/all-crime?" + urlencode(query), "police.uk", 8.0)
        return data or []

    def _venues(self, lat: float, lon: float, miles: float) -> list[tuple[float, float]] | None:
        """Pubs, bars, clubs and takeaways from the FSA register: where people are.

        Best-effort: None if the register cannot be reached, and the answer
        goes out without a venue count.
        """
        found: dict[str, tuple[float, float]] = {}
        for business_type in ("7843", "7844"):
            query = {"latitude": f"{lat:.5f}", "longitude": f"{lon:.5f}",
                     "maxDistanceLimit": f"{miles:.2f}", "businessTypeId": business_type,
                     "pageSize": "1000"}
            try:
                data = self._get_json(
                    "https://api.ratings.food.gov.uk/Establishments?" + urlencode(query),
                    "FSA", 4.0, {"x-api-version": "2", "Accept": "application/json"})
            except AgentAppError:
                return None
            for e in (data or {}).get("establishments") or []:
                g = e.get("geocode") or {}
                try:
                    found[str(e.get("FHRSID"))] = (float(g["latitude"]), float(g["longitude"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return list(found.values())

    @staticmethod
    def _summarise(crimes: list[dict]) -> dict:
        counts = Counter(c.get("category", "other-crime") for c in crimes)
        top = ", ".join(f"{LABELS.get(k, k.replace('-', ' '))} {n}" for k, n in counts.most_common(3))
        return {
            "crimes_total": len(crimes),
            "violence_and_robbery": sum(counts[k] for k in VIOLENT),
            "theft_from_person": counts["theft-from-the-person"],
            "top_categories": top or "none recorded",
        }

    # ── handle ────────────────────────────────────────────────────────────
    def handle(self, call: Call) -> Result:
        if call.outcome_id == "area.check":
            return self._area(call)
        return self._walk(call)

    def _area(self, call: Call) -> Result:
        place = (call.inputs.get("place") or "").strip()
        if not place:
            raise NeedAPlace("no place in the message")
        lat, lon, name = self._geocode(place)
        month = self._month()
        dlat, dlon = _offsets(lat, AREA_M)
        box = [(lat + dlat, lon - dlon), (lat + dlat, lon + dlon),
               (lat - dlat, lon + dlon), (lat - dlat, lon - dlon)]
        stats = self._summarise(self._crimes(box, month))
        local = call.context.now.astimezone(LONDON)
        sunrise, sunset = sun_times(lat, lon, local.date())
        venues = self._venues(lat, lon, AREA_M / 1609 + 0.05)
        count = None if venues is None else sum(
            1 for v in venues if haversine_km(lat, lon, v[0], v[1]) * 1000 <= AREA_M)
        busy = busyness(count, local.hour, busy_at=20, some_at=6)
        data = {"place_used": name, **stats, "month_used": month,
                "radius_m": AREA_M, "sunset": sunset, "busyness": busy}
        if count is not None:
            data["late_venues"] = count
        narrative = (f"Police recorded {stats['crimes_total']} street crimes within about "
                     f"{AREA_M} m of {name} in {month}, {stats['violence_and_robbery']} of them "
                     f"violence or robbery. Right now it is {busy}. Sunset there is {sunset}.")
        return Result(data=data, narrative=narrative)

    def _walk(self, call: Call) -> Result:
        start = (call.inputs.get("from_place") or "").strip()
        end = (call.inputs.get("to_place") or "").strip()
        if not start or not end:
            raise NeedTwoPlaces(f"from={start!r} to={end!r}")
        a_lat, a_lon, a_name = self._geocode(start)
        b_lat, b_lon, b_name = self._geocode(end, first=False)
        km = haversine_km(a_lat, a_lon, b_lat, b_lon)
        if km > MAX_WALK_KM:
            raise WalkTooLong(f"{km:.1f} km")
        route_km = km * 1.3
        minutes = max(1, round(route_km / 5.0 * 60))

        local = call.context.now.astimezone(LONDON)
        walk_at = local
        match = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", call.inputs.get("time") or "")
        if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
            walk_at = local.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
            if walk_at < local - timedelta(hours=2):
                walk_at += timedelta(days=1)
        sunrise, sunset = sun_times(a_lat, a_lon, walk_at.date())
        clock = walk_at.strftime("%H:%M")
        dark = not (sunrise <= clock < sunset)

        month = self._month()
        stats = self._summarise(self._crimes(corridor(a_lat, a_lon, b_lat, b_lon, CORRIDOR_M), month))
        venues = self._venues((a_lat + b_lat) / 2, (a_lon + b_lon) / 2, min(2.0, km / 2 / 1.609 + 0.1))
        count = None if venues is None else sum(
            1 for v in venues if dist_to_line_m(v[0], v[1], [(a_lat, a_lon), (b_lat, b_lon)]) <= CORRIDOR_M)
        per_km = max(route_km, 0.1)
        busy = busyness(count, walk_at.hour, busy_at=8 * per_km, some_at=3 * per_km)
        data = {"from_used": a_name, "to_used": b_name, "distance_km": round(route_km, 2),
                "walk_minutes": minutes, **stats, "month_used": month, "walk_time": clock,
                "dark": dark, "sunset": sunset, "sunrise": sunrise, "busyness": busy}
        if count is not None:
            data["late_venues"] = count
        light = "in the dark" if dark else "in daylight"
        narrative = (f"About {minutes} minutes ({route_km:.1f} km) from {a_name} to {b_name}, "
                     f"{light} at {clock} (sunset {sunset}). Police recorded "
                     f"{stats['crimes_total']} street crimes along that line in {month}, "
                     f"{stats['violence_and_robbery']} violence or robbery and "
                     f"{stats['theft_from_person']} theft from the person. At that hour the "
                     f"route is {busy}.")
        return Result(data=data, narrative=narrative)


# ── geometry and sun ──────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _offsets(lat: float, metres: float) -> tuple[float, float]:
    return metres / 111_320.0, metres / (111_320.0 * math.cos(math.radians(lat)))


def corridor(a_lat, a_lon, b_lat, b_lon, half_width_m):
    """A rectangle of half_width_m either side of the straight line A to B."""
    k = 111_320.0 * math.cos(math.radians((a_lat + b_lat) / 2))
    ax, ay, bx, by = a_lon * k, a_lat * 111_320.0, b_lon * k, b_lat * 111_320.0
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    nx, ny = -uy * half_width_m, ux * half_width_m
    ex, ey = ux * half_width_m, uy * half_width_m
    corners = [(ax - ex + nx, ay - ey + ny), (bx + ex + nx, by + ey + ny),
               (bx + ex - nx, by + ey - ny), (ax - ex - nx, ay - ey - ny)]
    return [(y / 111_320.0, x / k) for x, y in corners]


def sun_times(lat: float, lon: float, day) -> tuple[str, str]:
    """Sunrise and sunset, HH:MM UK time, from the NOAA approximation."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    def clock(minutes: float) -> str:
        return (midnight + timedelta(minutes=minutes)).astimezone(LONDON).strftime("%H:%M")

    return clock(720 - 4 * (lon + ha) - eqtime), clock(720 - 4 * (lon - ha) - eqtime)


def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    return lon * 111_320.0 * math.cos(math.radians(lat0)), lat * 111_320.0


def dist_to_line_m(lat: float, lon: float, coords: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a polyline."""
    px, py = _xy(lat, lon, lat)
    pts = [_xy(a, b, lat) for a, b in coords]
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def busyness(venues: int | None, hour: int, busy_at: float, some_at: float) -> str:
    """How many people are likely about, from late venues and the hour."""
    if 1 <= hour < 6:
        return "likely quiet: most pubs and takeaways are shut at that hour"
    if venues is None:
        return "busyness unknown"
    if venues >= busy_at:
        level = "busy"
    elif venues >= some_at:
        level = "fairly busy"
    else:
        level = "quiet"
    return f"{level} ({venues} pubs, bars and takeaways)"
