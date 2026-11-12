#!/usr/bin/env python3
"""The watcher of the Home Doctors status page.

One run asks six public addresses, appends one line to
data/history/<yyyy-mm>.jsonl and rewrites data/summary.json, which
index.html reads. Python 3 standard library only.

What a run never stores: the build version that the health answer
carries, the list of internal checks that the readiness answer carries,
and any field of the status feed that is not in its version 1 contract.

The functions that compute uptime, days and incidents are pure: they take
the history rows (or the points of one series) and a time, and they
return plain data. `http_get` is the one function that touches the
network, and `wait` is the one function that sleeps.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "hd-status-watcher"
TIMEOUT_SECONDS = 10
RETRY_WAIT_SECONDS = 5
MAX_BODY_BYTES = 65536

# Our three public host names. The page and the data name no other host.
APP = "https://app.homedoctors4u.com"  # the website: staff and providers
AGENCY = "https://hhn.homedoctors4u.com"  # the agency portal
SITE = "https://www.homedoctors4u.com"  # our public website

# What a run asks, in order: (key, address).
PROBES = (
    ("app", APP + "/api/health"),
    ("ready", APP + "/api/health/ready"),
    ("signin", APP + "/login"),
    ("agency", AGENCY + "/hhn/login"),
    ("feed", APP + "/api/status"),
    ("site", SITE),
)

# What the page lists, in order: (probe key, name on the page).
COMPONENTS = (
    ("app", "Website"),
    ("signin", "Staff and provider sign-in"),
    ("agency", "Agency portal sign-in"),
    ("site", "Our public website"),
)

# These pages are parts of the website. When the website itself does not
# answer, their failed checks are the same outage, not incidents of their own.
PARTS_OF_THE_WEBSITE = ("signin", "agency")

READY_NAME = "Website (a part is not ready)"
READY_NOTE = "The website answers, but a part of it is not ready."

HEADLINES = {
    "ok": "All services are working.",
    "trouble": "Some services have trouble.",
    "down": "The website is not answering.",
}

FEED_VERSION = 1
FEED_MAX_COMPONENTS = 12
FEED_NAME_MAX = 40
FEED_NOTE_MAX = 200
_FEED_ID = re.compile(r"[a-z0-9_-]{1,32}")
_READY_STATUS = re.compile(r"[a-z_]{1,20}")

MIN_CHECKS = 12  # fewer checks in a window: "not enough data yet"
MIN_INCIDENT_CHECKS = 2  # one failed check alone can be the watcher's network
BAD_DAY_SHARE = 0.10  # a day is bad from this share of failed checks
STRIP_DAYS = 90
STALE_AFTER_MINUTES = 20  # the page reads this number from the summary
MAX_INCIDENTS = 25
WINDOWS = (
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
    ("30d", timedelta(days=30)),
    ("90d", timedelta(days=90)),
)

try:
    from zoneinfo import ZoneInfo

    DAY_ZONE = ZoneInfo("America/Chicago")
    DAY_ZONE_NAME = "America/Chicago"
except Exception:  # no time zone database on this machine
    DAY_ZONE = timezone.utc
    DAY_ZONE_NAME = "UTC"


# --------------------------------------------------------------------------
# The network: one function, and one function that sleeps.
# --------------------------------------------------------------------------

# An opener of our own: no cookie handler, so no cookie is kept or sent.
_OPENER = urllib.request.build_opener()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, method="GET"
    )


def http_get(url: str, timeout: float = TIMEOUT_SECONDS) -> tuple[int, bytes]:
    """GET one address. Return (HTTP status, the first bytes of the body).

    Redirects are followed. An address that does not answer raises OSError
    or http.client.HTTPException. Tests replace this function.
    """
    try:
        with _OPENER.open(_request(url), timeout=timeout) as response:
            return response.status, response.read(MAX_BODY_BYTES)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read(MAX_BODY_BYTES)


def wait(seconds: float) -> None:
    """Sleep. Tests replace this function."""
    time.sleep(seconds)


def probe(url: str) -> tuple[int, bytes, int]:
    """Ask one address, with one retry after 5 seconds for no answer or a 5xx.

    Return (HTTP status, body, milliseconds of the last try). The status is
    0 when the address did not answer (a timeout or a failed connection).
    """
    status, body, ms = 0, b"", 0
    for attempt in (1, 2):
        start = time.monotonic()
        try:
            status, body = http_get(url)
        except (OSError, http.client.HTTPException):
            status, body = 0, b""
        ms = int((time.monotonic() - start) * 1000)
        if attempt == 1 and (status == 0 or status >= 500):
            wait(RETRY_WAIT_SECONDS)
            continue
        break
    return status, body, ms


# --------------------------------------------------------------------------
# From answers to one history row. Pure.
# --------------------------------------------------------------------------


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: object) -> datetime | None:
    """Read an ISO time. A time with no zone is UTC. Anything else: None."""
    if not isinstance(text, str) or not text or len(text) > 40:
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _json_object(body: bytes) -> dict | None:
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _plain(text: object, limit: int) -> str:
    """One line of text with no control characters, cut to a limit."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())[:limit].strip()


def read_ready(body: bytes) -> dict | None:
    """Keep only the top-level `status` and `degraded` of the readiness
    answer. Its list of internal checks is never read into the result."""
    data = _json_object(body)
    if data is None:
        return None
    kept: dict = {}
    status = data.get("status")
    if isinstance(status, str) and _READY_STATUS.fullmatch(status):
        kept["status"] = status
    if isinstance(data.get("degraded"), bool):
        kept["degraded"] = data["degraded"]
    return kept or None


def read_feed(status: int, body: bytes) -> list[dict] | None:
    """Read the app's public status feed, contract version 1.

    Return its components, or None when the feed is not available: a 404,
    an answer that is not JSON, or a version that is not 1. A field that
    the contract does not name is ignored.
    """
    if status != 200:
        return None
    data = _json_object(body)
    if data is None:
        return None
    version = data.get("version")
    if isinstance(version, bool) or version != FEED_VERSION:
        return None
    components = data.get("components")
    if not isinstance(components, list):
        return None
    kept: list[dict] = []
    seen: set[str] = set()
    for item in components:
        if len(kept) >= FEED_MAX_COMPONENTS:
            break
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        name = _plain(item.get("name"), FEED_NAME_MAX)
        state = item.get("state")
        if not isinstance(ident, str) or not _FEED_ID.fullmatch(ident):
            continue
        if ident in seen or not name or state not in ("ok", "trouble"):
            continue
        seen.add(ident)
        entry: dict = {"id": ident, "name": name, "ok": int(state == "ok")}
        if state == "trouble":
            since = parse_time(item.get("since"))
            if since is not None:
                entry["since"] = iso(since)
            note = _plain(item.get("note"), FEED_NOTE_MAX)
            if note:
                entry["note"] = note
        kept.append(entry)
    return kept


def build_row(now: datetime, answers: dict[str, tuple[int, bytes, int]]) -> dict:
    """Make the one history row of a run from the six answers.

    `p` holds, per probe, [ok as 1 or 0, HTTP status, milliseconds].
    """
    probes: dict[str, list[int]] = {}
    row: dict = {"t": iso(now), "p": probes}
    for key, _url in PROBES:
        status, body, ms = answers.get(key, (0, b"", 0))
        if key == "app":
            data = _json_object(body) if status == 200 else None
            ok = data is not None and data.get("status") == "healthy"
        elif key == "feed":
            feed = read_feed(status, body)
            ok = feed is not None
            if feed is not None:
                row["feed"] = feed
        else:
            ok = status == 200
            if key == "ready":
                ready = read_ready(body)
                if ready is not None:
                    row["ready"] = ready
        probes[key] = [int(ok), int(status), int(ms)]
    return row


def run_checks(now: datetime) -> dict:
    """Ask every address and return the history row of this run."""
    return build_row(now, {key: probe(url) for key, url in PROBES})


# --------------------------------------------------------------------------
# From history rows to the numbers of the page. Pure.
#
# A point is (time, ok, hint). `ok` is True, False, or None when the run
# could not know (the feed was not there). `hint` is the start of the
# trouble as the feed gave it, or None.
# --------------------------------------------------------------------------


def series(rows: list[dict], key: str) -> list[tuple]:
    """The points of one series, oldest first.

    `key` is a probe key ("app", "signin", "agency", "site"), "ready", or
    "feed:<id>" for a component of the feed. The "ready" series is known
    only while the app answers, so one outage is not counted twice.
    """
    points = []
    for row in rows:
        moment = parse_time(row.get("t"))
        if moment is None:
            continue
        probes = row.get("p") or {}
        ok: bool | None = None
        hint = None
        if key.startswith("feed:"):
            for item in row.get("feed") or ():
                if item.get("id") == key[5:]:
                    ok = bool(item.get("ok"))
                    hint = parse_time(item.get("since"))
        elif key == "ready":
            if (probes.get("app") or [0])[0] and "ready" in probes:
                ok = bool(probes["ready"][0])
        elif key in probes:
            ok = bool(probes[key][0])
        points.append((moment, ok, hint))
    points.sort(key=lambda point: point[0])
    return points


def _known(points: list[tuple]) -> list[tuple]:
    return [point for point in points if point[1] is not None]


def failure_runs(points: list[tuple]) -> list[dict]:
    """Every run of failed checks in a row: start, end (the first good
    check after it, or None while it is still open) and the times."""
    runs: list[dict] = []
    current: dict | None = None
    previous_ok: datetime | None = None
    for moment, ok, hint in _known(points):
        if ok:
            if current is not None:
                current["end"] = moment
                runs.append(current)
                current = None
            previous_ok = moment
            continue
        if current is None:
            start = moment
            # The feed knows when the trouble began. Believe it only when
            # it is not later than our check and not before our last good one.
            if hint is not None and hint <= moment:
                if previous_ok is None or hint > previous_ok:
                    start = hint
            current = {"start": start, "end": None, "times": []}
        current["times"].append(moment)
    if current is not None:
        runs.append(current)
    return runs


def incidents(points: list[tuple], now: datetime) -> list[dict]:
    """An incident is 2 or more failed checks in a row. Oldest first."""
    found = []
    for run in failure_runs(points):
        if len(run["times"]) < MIN_INCIDENT_CHECKS:
            continue
        until = run["end"] or now
        minutes = max(0, round((until - run["start"]).total_seconds() / 60))
        found.append({"start": run["start"], "end": run["end"], "minutes": minutes})
    return found


def inside(incident: dict, others: list[dict]) -> bool:
    """True when the incident lies inside one of the others: it began no
    earlier and it ended no later."""
    for other in others:
        if other["start"] > incident["start"]:
            continue
        if other["end"] is None:
            return True
        if incident["end"] is not None and incident["end"] <= other["end"]:
            return True
    return False


def current_state(points: list[tuple]) -> tuple[str | None, datetime | None]:
    """The state at the newest check: ("ok", None), ("trouble", since), or
    (None, None) when there is no check. The newest check decides, also
    when it is the first failed one."""
    known = _known(points)
    if not known:
        return None, None
    if known[-1][1]:
        return "ok", None
    return "trouble", failure_runs(points)[-1]["start"]


def uptime(points: list[tuple], now: datetime, window: timedelta) -> dict:
    """The share of checks that were ok in the window that ends at `now`.

    `pct` is None until the window holds MIN_CHECKS checks. It is cut, not
    rounded, so a window with one failed check never reads 100.
    """
    inside = [ok for moment, ok, _ in _known(points) if now - window < moment <= now]
    total = len(inside)
    if total < MIN_CHECKS:
        return {"checks": total, "pct": None}
    good = sum(1 for ok in inside if ok)
    return {"checks": total, "pct": math.floor(good * 10000 / total) / 100}


def day_strip(points: list[tuple], today: date, days: int = STRIP_DAYS) -> str:
    """One letter per day, oldest first, ending today: g good, p partial,
    b bad, n no checks. Days are days of DAY_ZONE.

    A failed check counts only inside a run of 2 or more, as for an
    incident, or in the run that is still open, so that today agrees with
    the state of the component. A day is bad when 10% or more of its checks
    failed that way.
    """
    confirmed = set()
    for run in failure_runs(points):
        if len(run["times"]) >= MIN_INCIDENT_CHECKS or run["end"] is None:
            confirmed.update(run["times"])
    checks: dict[date, int] = {}
    failed: dict[date, int] = {}
    for moment, _ok, _hint in _known(points):
        day = moment.astimezone(DAY_ZONE).date()
        checks[day] = checks.get(day, 0) + 1
        if moment in confirmed:
            failed[day] = failed.get(day, 0) + 1
    letters = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        total = checks.get(day, 0)
        bad = failed.get(day, 0)
        if total == 0:
            letters.append("n")
        elif bad == 0:
            letters.append("g")
        elif bad / total >= BAD_DAY_SHARE:
            letters.append("b")
        else:
            letters.append("p")
    return "".join(letters)


def summarize(rows: list[dict], now: datetime) -> dict:
    """Everything the page shows, from the history rows. `now` is the time
    of the newest check."""
    oldest = now - timedelta(days=STRIP_DAYS + 1)
    rows = [row for row in rows if (parse_time(row.get("t")) or oldest) > oldest]
    rows.sort(key=lambda row: row.get("t") or "")
    today = now.astimezone(DAY_ZONE).date()
    latest = rows[-1] if rows else {}

    feed_names: dict[str, str] = {}
    for row in rows:
        for item in row.get("feed") or ():
            feed_names[item["id"]] = item["name"]
    latest_feed = latest.get("feed")

    def component(ident: str, name: str, points: list[tuple]) -> dict:
        state, since = current_state(points)
        return {
            "id": ident,
            "name": name,
            "state": state,
            "since": iso(since) if since else None,
            "note": None,
            "uptime": {label: uptime(points, now, span) for label, span in WINDOWS},
            "days": day_strip(points, today),
        }

    components = []
    listed: list[tuple[str, str, list[tuple]]] = []
    for key, name in COMPONENTS:
        points = series(rows, key)
        components.append(component(key, name, points))
        listed.append((key, name, points))

    ready_points = series(rows, "ready")
    ready_state, ready_since = current_state(ready_points)
    listed.append(("ready", READY_NAME, ready_points))
    website = components[0]
    if website["state"] == "ok" and ready_state == "trouble":
        website.update(state="trouble", since=iso(ready_since), note=READY_NOTE)

    for item in latest_feed or ():
        points = series(rows, "feed:" + item["id"])
        entry = component(item["id"], item["name"], points)
        if entry["state"] == "trouble":
            entry["note"] = item.get("note")
        components.append(entry)
    for ident, name in feed_names.items():
        listed.append((ident, name, series(rows, "feed:" + ident)))

    website_incidents = incidents(listed[0][2], now)
    past = []
    for ident, name, points in listed:
        for incident in incidents(points, now):
            if ident in PARTS_OF_THE_WEBSITE and inside(incident, website_incidents):
                continue
            past.append(
                {
                    "id": ident,
                    "name": name,
                    "start": iso(incident["start"]),
                    "end": iso(incident["end"]) if incident["end"] else None,
                    "minutes": incident["minutes"],
                }
            )
    past.sort(key=lambda incident: incident["start"], reverse=True)
    past.sort(key=lambda incident: incident["end"] is not None)  # open ones first

    app_state = current_state(series(rows, "app"))[0]
    if app_state == "trouble":
        overall = "down"
    elif any(entry["state"] == "trouble" for entry in components):
        overall = "trouble"
    else:
        overall = "ok"

    ready_kept = latest.get("ready") or {}
    return {
        "version": 1,
        "checked_at": iso(now),
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "overall": overall,
        "headline": HEADLINES[overall],
        "feed": latest_feed is not None,
        "ready": {
            "ok": ready_state == "ok" if ready_state else None,
            "degraded": ready_kept.get("degraded"),
        },
        "day_zone": DAY_ZONE_NAME,
        "days_end": today.isoformat(),
        "components": components,
        "incidents": past[:MAX_INCIDENTS],
    }


# --------------------------------------------------------------------------
# Files.
# --------------------------------------------------------------------------


def history_path(data_dir: Path, moment: datetime) -> Path:
    return data_dir / "history" / (moment.astimezone(timezone.utc).strftime("%Y-%m") + ".jsonl")


def append_row(data_dir: Path, row: dict) -> Path:
    path = history_path(data_dir, parse_time(row["t"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
    return path


def load_history(data_dir: Path, now: datetime) -> list[dict]:
    """The rows of this month and the three months before it, which always
    cover 90 days. A line that is not a JSON object is skipped."""
    rows: list[dict] = []
    year, month = now.year, now.month
    for _ in range(4):
        path = data_dir / "history" / f"{year:04d}-{month:02d}.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and parse_time(row.get("t")):
                    rows.append(row)
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return rows


def write_summary(data_dir: Path, summary: dict) -> Path:
    path = data_dir / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="the folder that holds history/ and summary.json",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="ask nothing; rebuild summary.json from the history",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    if args.summary_only:
        rows = load_history(args.data, now)
        if not rows:
            print("no history yet", file=sys.stderr)
            return 1
        now = max(parse_time(row["t"]) for row in rows)
    else:
        append_row(args.data, run_checks(now))
        rows = load_history(args.data, now)
    summary = summarize(rows, now)
    write_summary(args.data, summary)
    # A service in trouble is a result, not a failure of the watcher: exit 0.
    print(f"{summary['checked_at']} {summary['overall']}: {summary['headline']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
