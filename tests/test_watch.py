"""Tests of the watcher. No test touches the network: every one replaces
`watch.http_get` (the one network function) and `watch.wait` (the one sleep).

Run: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import watch  # noqa: E402

T0 = datetime(2026, 11, 12, 18, 0, 0, tzinfo=timezone.utc)  # 12:00 PM Central
STEP = timedelta(minutes=5)

HEALTH_BODY = json.dumps(
    {"status": "healthy", "version": "9.87.6-build-abc1234", "database": "connected"}
).encode()

# The names in this body are made up. The real readiness answer names
# internal checks, and this repository is public, so it does not repeat them.
READY_BODY = json.dumps(
    {
        "status": "ready",
        "degraded": True,
        "checks": [
            {"name": "database", "ok": True},
            {"name": "acmefax_gateway", "ok": True},
            {"name": "globex_mail", "ok": False},
            {"name": "initech-storage", "ok": True},
            {"name": "umbrella_pdf_engine", "ok": True},
        ],
    }
).encode()
READY_STRINGS = ("acmefax", "globex", "initech", "umbrella", "database", "gateway", "engine")

FEED_OK = {
    "version": 1,
    "status": "ok",
    "components": [
        {"id": "email", "name": "Email", "state": "ok"},
        {"id": "fax", "name": "Fax", "state": "ok"},
        {"id": "texts", "name": "Text messages", "state": "ok"},
    ],
}
FEED_FAX_TROUBLE = {
    "version": 1,
    "status": "trouble",
    "extra_top_level": {"anything": "ignored"},
    "components": [
        {"id": "email", "name": "Email", "state": "ok"},
        {
            "id": "fax",
            "name": "Fax",
            "state": "trouble",
            "since": "2026-11-12T17:45:02Z",
            "note": "Our fax service has a fault right now.",
            "internal_key": "must-not-be-kept",
        },
        {"id": "texts", "name": "Text messages", "state": "ok"},
    ],
}


def answers(feed=None, **overrides):
    """The six answers of a run in which everything works and the feed is
    not there yet (404), with overrides per probe key."""
    result = {
        "app": (200, HEALTH_BODY, 120),
        "ready": (200, READY_BODY, 90),
        "signin": (200, b"<html>sign in</html>", 80),
        "agency": (200, b"<html>agency sign in</html>", 85),
        "feed": (404, b"<html>Not Found</html>", 40),
        "site": (200, b"<html>home</html>", 300),
    }
    if feed is not None:
        result["feed"] = (200, json.dumps(feed).encode(), 45)
    result.update(overrides)
    return result


def row(index, feed=None, **failed):
    """A history row at T0 + index * 5 minutes. `app=0` fails that probe."""
    overrides = {}
    for key, value in failed.items():
        if not value:
            overrides[key] = (503, b"", 30)
    return watch.build_row(T0 + index * STEP, answers(feed=feed, **overrides))


def points(*oks, start=T0, step=STEP):
    return [(start + index * step, ok, None) for index, ok in enumerate(oks)]


class UptimeWindows(unittest.TestCase):
    def test_not_enough_data_until_twelve_checks(self):
        eleven = points(*[True] * 11)
        now = eleven[-1][0]
        self.assertEqual(watch.uptime(eleven, now, timedelta(hours=24)), {"checks": 11, "pct": None})
        twelve = points(*[True] * 12)
        now = twelve[-1][0]
        self.assertEqual(watch.uptime(twelve, now, timedelta(hours=24)), {"checks": 12, "pct": 100.0})

    def test_share_of_ok_checks(self):
        twenty = points(*([True] * 15 + [False] * 5))
        got = watch.uptime(twenty, twenty[-1][0], timedelta(hours=24))
        self.assertEqual(got, {"checks": 20, "pct": 75.0})

    def test_each_window_counts_only_its_own_checks(self):
        now = T0
        # One check an hour for 100 days, newest at `now`. The checks older
        # than 30 days all failed.
        series = []
        for hours_back in range(100 * 24):
            moment = now - timedelta(hours=hours_back)
            series.append((moment, hours_back < 30 * 24, None))
        series.sort()
        got = {label: watch.uptime(series, now, span) for label, span in watch.WINDOWS}
        self.assertEqual(got["24h"], {"checks": 24, "pct": 100.0})
        self.assertEqual(got["7d"], {"checks": 7 * 24, "pct": 100.0})
        self.assertEqual(got["30d"], {"checks": 30 * 24, "pct": 100.0})
        self.assertEqual(got["90d"]["checks"], 90 * 24)
        self.assertEqual(got["90d"]["pct"], 33.33)

    def test_a_failed_check_never_reads_100(self):
        many = points(*([True] * 99_999 + [False]), step=timedelta(seconds=1))
        got = watch.uptime(many, many[-1][0], timedelta(days=90))
        self.assertEqual(got["pct"], 99.99)

    def test_a_check_that_could_not_know_is_not_counted(self):
        mixed = points(*([True] * 12 + [None] * 5))
        got = watch.uptime(mixed, mixed[-1][0], timedelta(hours=24))
        self.assertEqual(got, {"checks": 12, "pct": 100.0})

    def test_the_summary_carries_the_four_windows(self):
        rows = [row(index) for index in range(12)]
        summary = watch.summarize(rows, T0 + 11 * STEP)
        website = summary["components"][0]
        self.assertEqual(list(website["uptime"]), ["24h", "7d", "30d", "90d"])
        self.assertEqual(website["uptime"]["24h"], {"checks": 12, "pct": 100.0})


class Incidents(unittest.TestCase):
    def test_one_failed_check_alone_is_no_incident(self):
        series = points(True, True, False, True, True)
        self.assertEqual(watch.incidents(series, series[-1][0]), [])

    def test_two_failed_checks_in_a_row_are_an_incident(self):
        series = points(True, False, False, True, True)
        got = watch.incidents(series, series[-1][0])
        self.assertEqual(
            got, [{"start": T0 + STEP, "end": T0 + 3 * STEP, "minutes": 10}]
        )

    def test_an_incident_that_is_still_open(self):
        series = points(True, True, False, False, False)
        now = series[-1][0]
        got = watch.incidents(series, now)
        self.assertEqual(got, [{"start": T0 + 2 * STEP, "end": None, "minutes": 10}])
        self.assertEqual(watch.current_state(series), ("trouble", T0 + 2 * STEP))

    def test_the_open_incident_is_in_the_summary_and_changes_the_headline(self):
        rows = [row(0), row(1), row(2, signin=0), row(3, signin=0)]
        summary = watch.summarize(rows, T0 + 3 * STEP)
        self.assertEqual(summary["overall"], "trouble")
        self.assertEqual(summary["headline"], "Some services have trouble.")
        signin = summary["components"][1]
        self.assertEqual(signin["name"], "Staff and provider sign-in")
        self.assertEqual(signin["state"], "trouble")
        self.assertEqual(signin["since"], watch.iso(T0 + 2 * STEP))
        self.assertEqual(
            summary["incidents"],
            [
                {
                    "id": "signin",
                    "name": "Staff and provider sign-in",
                    "start": watch.iso(T0 + 2 * STEP),
                    "end": None,
                    "minutes": 5,
                }
            ],
        )

    def test_a_closed_incident_has_its_end_and_its_length(self):
        rows = [row(0), row(1, site=0), row(2, site=0), row(3, site=0), row(4)]
        summary = watch.summarize(rows, T0 + 4 * STEP)
        self.assertEqual(summary["overall"], "ok")
        self.assertEqual(len(summary["incidents"]), 1)
        incident = summary["incidents"][0]
        self.assertEqual(incident["name"], "Our public website")
        self.assertEqual(incident["end"], watch.iso(T0 + 4 * STEP))
        self.assertEqual(incident["minutes"], 15)

    def test_the_first_failed_check_already_shows_trouble_but_is_no_incident(self):
        rows = [row(0), row(1), row(2, agency=0)]
        summary = watch.summarize(rows, T0 + 2 * STEP)
        self.assertEqual(summary["components"][2]["state"], "trouble")
        self.assertEqual(summary["incidents"], [])

    def test_the_app_down_reads_the_website_is_not_answering(self):
        down = {"app": 0, "ready": 0, "signin": 0, "agency": 0}
        rows = [row(0), row(1, **down), row(2, **down)]
        summary = watch.summarize(rows, T0 + 2 * STEP)
        self.assertEqual(summary["overall"], "down")
        self.assertEqual(summary["headline"], "The website is not answering.")
        # One outage is one incident: the pages of the website and its
        # readiness answer do not each add their own.
        self.assertEqual([incident["id"] for incident in summary["incidents"]], ["app"])
        self.assertEqual(
            [c["state"] for c in summary["components"]], ["trouble", "trouble", "trouble", "ok"]
        )

    def test_a_sign_in_incident_that_began_before_the_website_went_down_is_listed(self):
        down = {"app": 0, "ready": 0, "signin": 0, "agency": 0}
        rows = [row(0), row(1, signin=0), row(2, **down), row(3, **down), row(4)]
        summary = watch.summarize(rows, T0 + 4 * STEP)
        self.assertEqual(
            sorted(incident["id"] for incident in summary["incidents"]), ["app", "signin"]
        )

    def test_inside(self):
        outer = [{"start": T0, "end": T0 + 4 * STEP}]
        self.assertTrue(watch.inside({"start": T0, "end": T0 + 4 * STEP}, outer))
        self.assertTrue(watch.inside({"start": T0 + STEP, "end": T0 + 2 * STEP}, outer))
        self.assertFalse(watch.inside({"start": T0 - STEP, "end": T0 + 2 * STEP}, outer))
        self.assertFalse(watch.inside({"start": T0 + STEP, "end": T0 + 5 * STEP}, outer))
        self.assertFalse(watch.inside({"start": T0 + STEP, "end": None}, outer))
        self.assertTrue(watch.inside({"start": T0 + STEP, "end": None}, [{"start": T0, "end": None}]))

    def test_not_ready_while_the_app_answers_is_trouble_on_the_website(self):
        rows = [row(0), row(1, ready=0), row(2, ready=0)]
        summary = watch.summarize(rows, T0 + 2 * STEP)
        website = summary["components"][0]
        self.assertEqual(summary["overall"], "trouble")
        self.assertEqual(website["state"], "trouble")
        self.assertEqual(website["since"], watch.iso(T0 + STEP))
        self.assertEqual(website["note"], watch.READY_NOTE)
        self.assertEqual([i["id"] for i in summary["incidents"]], ["ready"])

    def test_open_incidents_come_first_then_the_newest(self):
        rows = [
            row(0, site=0),
            row(1, site=0),
            row(2),
            row(3, agency=0),
            row(4, agency=0),
            row(5, signin=0),
            row(6, signin=0),
        ]
        summary = watch.summarize(rows, T0 + 6 * STEP)
        self.assertEqual([i["id"] for i in summary["incidents"]], ["signin", "agency", "site"])


class DayStrip(unittest.TestCase):
    TODAY = date(2026, 11, 12)

    def test_ninety_days_and_one_day_of_data(self):
        strip = watch.day_strip(points(True, True, True), self.TODAY)
        self.assertEqual(len(strip), 90)
        self.assertEqual(strip, "n" * 89 + "g")

    def test_good_partial_and_bad_days(self):
        day = timedelta(days=1)
        series = (
            points(*[True] * 20, start=T0 - 3 * day)  # good
            + points(*([True] * 18 + [False] * 2), start=T0 - 2 * day)  # 10%: bad
            + points(*([True] * 28 + [False] * 2), start=T0 - day)  # under 10%: partial
            + points(*[True] * 5, start=T0)
        )
        strip = watch.day_strip(series, self.TODAY)
        self.assertEqual(strip[-4:], "gbpg")
        self.assertEqual(strip[:-4], "n" * 86)

    def test_one_failed_check_alone_leaves_the_day_good(self):
        strip = watch.day_strip(points(True, False, True, True), self.TODAY)
        self.assertEqual(strip[-1], "g")

    def test_the_first_failed_check_marks_today_while_it_is_still_open(self):
        strip = watch.day_strip(points(*([True] * 20 + [False])), self.TODAY)
        self.assertEqual(strip[-1], "p")

    def test_a_day_is_a_central_time_day(self):
        # 04:30 UTC on 11/13 is 10:30 PM Central on 11/12.
        late = datetime(2026, 11, 13, 4, 30, tzinfo=timezone.utc)
        strip = watch.day_strip(points(False, False, start=late), self.TODAY)
        self.assertEqual(strip[-1], "b")


class Feed(unittest.TestCase):
    def test_the_feed_absent_404(self):
        built = watch.build_row(T0, answers())
        self.assertNotIn("feed", built)
        self.assertEqual(built["p"]["feed"], [0, 404, 40])
        summary = watch.summarize([built], T0)
        self.assertFalse(summary["feed"])
        self.assertEqual(summary["overall"], "ok")
        self.assertEqual(summary["headline"], "All services are working.")
        self.assertEqual(
            [c["name"] for c in summary["components"]],
            [
                "Website",
                "Staff and provider sign-in",
                "Agency portal sign-in",
                "Our public website",
            ],
        )
        self.assertEqual(summary["incidents"], [])

    def test_an_answer_that_is_not_json_is_no_feed(self):
        for body in (b"<html>ok</html>", b"", b"[1, 2]", b'"ok"', b"\xff\xfe"):
            raw = answers()
            raw["feed"] = (200, body, 40)
            built = watch.build_row(T0, raw)
            self.assertNotIn("feed", built, body)
            self.assertEqual(built["p"]["feed"][0], 0)
            self.assertEqual(watch.summarize([built], T0)["overall"], "ok")

    def test_a_feed_body_under_another_status_is_no_feed(self):
        for status in (404, 500, 301):
            raw = answers()
            raw["feed"] = (status, json.dumps(FEED_FAX_TROUBLE).encode(), 40)
            built = watch.build_row(T0, raw)
            self.assertNotIn("feed", built, status)

    def test_the_feed_present_with_one_component_in_trouble(self):
        built = watch.build_row(T0, answers(feed=FEED_FAX_TROUBLE))
        self.assertEqual(built["p"]["feed"][0], 1)
        summary = watch.summarize([built], T0)
        self.assertTrue(summary["feed"])
        self.assertEqual(summary["overall"], "trouble")
        self.assertEqual(summary["headline"], "Some services have trouble.")
        names = [c["name"] for c in summary["components"]]
        self.assertEqual(names[4:], ["Email", "Fax", "Text messages"])
        fax = summary["components"][5]
        self.assertEqual(fax["state"], "trouble")
        self.assertEqual(fax["since"], "2026-11-12T17:45:02Z")
        self.assertEqual(fax["note"], "Our fax service has a fault right now.")
        email = summary["components"][4]
        self.assertEqual((email["state"], email["since"], email["note"]), ("ok", None, None))

    def test_fields_that_the_contract_does_not_name_are_ignored(self):
        built = watch.build_row(T0, answers(feed=FEED_FAX_TROUBLE))
        text = json.dumps(built)
        self.assertNotIn("internal_key", text)
        self.assertNotIn("must-not-be-kept", text)
        self.assertNotIn("extra_top_level", text)
        for item in built["feed"]:
            self.assertLessEqual(set(item), {"id", "name", "ok", "since", "note"})

    def test_a_feed_with_version_2_is_ignored(self):
        for version in (2, "1", None, True, 1.5):
            feed = dict(FEED_FAX_TROUBLE, version=version)
            built = watch.build_row(T0, answers(feed=feed))
            self.assertNotIn("feed", built, version)
            summary = watch.summarize([built], T0)
            self.assertFalse(summary["feed"])
            self.assertEqual(len(summary["components"]), 4)
            self.assertEqual(summary["overall"], "ok")

    def test_a_component_with_an_unknown_state_or_a_bad_shape_is_left_out(self):
        feed = {
            "version": 1,
            "components": [
                {"id": "email", "name": "Email", "state": "ok"},
                {"id": "fax", "name": "Fax", "state": "maintenance"},
                {"id": "Bad Id!", "name": "X", "state": "ok"},
                {"id": "email", "name": "Email again", "state": "trouble"},
                {"id": "noname", "name": "", "state": "ok"},
                "not a component",
            ],
        }
        built = watch.build_row(T0, answers(feed=feed))
        self.assertEqual(built["feed"], [{"id": "email", "name": "Email", "ok": 1}])

    def test_a_feed_incident_starts_when_the_feed_says_and_needs_two_checks(self):
        rows = [row(0, feed=FEED_OK), row(1, feed=FEED_FAX_TROUBLE)]
        summary = watch.summarize(rows, T0 + STEP)
        self.assertEqual(summary["incidents"], [])
        rows.append(row(2, feed=FEED_FAX_TROUBLE))
        rows.append(row(3, feed=FEED_OK))
        summary = watch.summarize(rows, T0 + 3 * STEP)
        self.assertEqual(len(summary["incidents"]), 1)
        incident = summary["incidents"][0]
        self.assertEqual(incident["name"], "Fax")
        # The feed's `since` (17:45:02) is before our last good check
        # (18:00), so the start is our own first failed check.
        self.assertEqual(incident["start"], watch.iso(T0 + STEP))
        self.assertEqual(incident["end"], watch.iso(T0 + 3 * STEP))

    def test_a_run_with_no_feed_neither_ends_nor_counts_in_a_feed_incident(self):
        rows = [
            row(0, feed=FEED_OK),
            row(1, feed=FEED_FAX_TROUBLE),
            row(2),  # the feed did not answer
            row(3, feed=FEED_FAX_TROUBLE),
        ]
        summary = watch.summarize(rows, T0 + 3 * STEP)
        self.assertEqual([i["name"] for i in summary["incidents"]], ["Fax"])
        fax = summary["components"][5]
        self.assertEqual(fax["uptime"]["24h"]["checks"], 3)


class NothingInternalIsStored(unittest.TestCase):
    def test_the_readiness_checks_are_never_stored(self):
        built = watch.build_row(T0, answers())
        self.assertNotIn("checks", built)
        self.assertEqual(built["ready"], {"status": "ready", "degraded": True})
        with tempfile.TemporaryDirectory() as folder:
            path = watch.append_row(Path(folder), built)
            watch.write_summary(Path(folder), watch.summarize([built], T0))
            line = path.read_text()
            summary = (Path(folder) / "summary.json").read_text()
        stored_row = json.loads(line)
        self.assertNotIn("checks", stored_row)
        self.assertEqual(sorted(stored_row), ["p", "ready", "t"])
        self.assertNotIn('"checks":[', line.replace(" ", ""))
        for word in READY_STRINGS:
            self.assertNotIn(word, line.lower(), word)
            self.assertNotIn(word, summary.lower(), word)

    def test_the_build_version_is_never_stored(self):
        built = watch.build_row(T0, answers())
        text = json.dumps(built) + json.dumps(watch.summarize([built], T0))
        for word in ("version\": \"9", "9.87.6", "abc1234", "connected"):
            self.assertNotIn(word, text, word)

    def test_a_strange_readiness_status_is_not_stored(self):
        body = json.dumps({"status": "Ready on host db-1.internal", "degraded": "yes"}).encode()
        built = watch.build_row(T0, answers(ready=(200, body, 50)))
        self.assertNotIn("ready", built)
        self.assertNotIn("internal", json.dumps(built))

    def test_the_app_is_up_only_when_it_says_healthy(self):
        for body, want in (
            (HEALTH_BODY, 1),
            (b'{"status": "unhealthy"}', 0),
            (b"<html>maintenance</html>", 0),
        ):
            built = watch.build_row(T0, answers(app=(200, body, 50)))
            self.assertEqual(built["p"]["app"][0], want, body)


class Requests(unittest.TestCase):
    def test_a_fixed_user_agent_and_no_cookie(self):
        request = watch._request("https://app.homedoctors4u.com/api/health")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("User-agent"), "hd-status-watcher")
        self.assertFalse(request.has_header("Cookie"))
        handlers = [type(handler).__name__ for handler in watch._OPENER.handlers]
        self.assertNotIn("HTTPCookieProcessor", handlers)

    def test_one_retry_after_five_seconds_for_a_timeout(self):
        calls = [TimeoutError("timed out"), (200, b"ok")]
        with mock.patch.object(watch, "http_get", side_effect=calls) as get, mock.patch.object(
            watch, "wait"
        ) as wait:
            status, body, _ms = watch.probe("https://example.invalid/")
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(get.call_count, 2)
        wait.assert_called_once_with(5)

    def test_one_retry_for_a_5xx_and_no_third_try(self):
        with mock.patch.object(watch, "http_get", return_value=(503, b"")) as get, mock.patch.object(
            watch, "wait"
        ) as wait:
            status, _body, _ms = watch.probe("https://example.invalid/")
        self.assertEqual(status, 503)
        self.assertEqual(get.call_count, 2)
        wait.assert_called_once_with(5)

    def test_no_retry_for_a_404(self):
        with mock.patch.object(watch, "http_get", return_value=(404, b"")) as get, mock.patch.object(
            watch, "wait"
        ) as wait:
            status, _body, _ms = watch.probe("https://example.invalid/")
        self.assertEqual(status, 404)
        self.assertEqual(get.call_count, 1)
        wait.assert_not_called()

    def test_no_answer_twice_is_status_0(self):
        with mock.patch.object(watch, "http_get", side_effect=OSError("down")), mock.patch.object(
            watch, "wait"
        ):
            self.assertEqual(watch.probe("https://example.invalid/")[:2], (0, b""))

    def test_the_six_addresses(self):
        self.assertEqual(
            [url for _key, url in watch.PROBES],
            [
                "https://app.homedoctors4u.com/api/health",
                "https://app.homedoctors4u.com/api/health/ready",
                "https://app.homedoctors4u.com/login",
                "https://app.homedoctors4u.com/hhn/login",
                "https://app.homedoctors4u.com/api/status",
                "https://www.homedoctors4u.com",
            ],
        )


class WholeRun(unittest.TestCase):
    def fake_get(self, url, timeout=10):
        self.asked.append(url)
        key = {address: key for key, address in watch.PROBES}[url]
        status, body, _ms = answers()[key]
        return status, body

    def test_a_run_appends_one_line_and_rewrites_the_summary(self):
        self.asked = []
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            watch, "http_get", side_effect=self.fake_get
        ), mock.patch.object(watch, "wait") as wait, contextlib.redirect_stdout(io.StringIO()):
            data = Path(folder) / "data"
            self.assertEqual(watch.main(["--data", str(data)]), 0)
            self.assertEqual(watch.main(["--data", str(data)]), 0)
            files = sorted((data / "history").iterdir())
            self.assertEqual(len(files), 1)
            self.assertRegex(files[0].name, r"^\d{4}-\d{2}\.jsonl$")
            lines = files[0].read_text().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(sorted(first), ["p", "ready", "t"])
            self.assertEqual(
                sorted(first["p"]), ["agency", "app", "feed", "ready", "signin", "site"]
            )
            summary = json.loads((data / "summary.json").read_text())
            self.assertEqual(summary["checked_at"], json.loads(lines[1])["t"])
            self.assertEqual(summary["overall"], "ok")
            self.assertFalse((data / "summary.json.tmp").exists())
        self.assertEqual(len(self.asked), 12)
        wait.assert_not_called()

    def test_a_broken_history_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            watch.append_row(data, row(0))
            with watch.history_path(data, T0).open("a") as handle:
                handle.write("{half a line\n\n[1]\n")
            watch.append_row(data, row(1))
            self.assertEqual(len(watch.load_history(data, T0)), 2)

    def test_the_history_reaches_back_over_the_year_end(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            january = datetime(2027, 1, 10, tzinfo=timezone.utc)
            for moment in (january, january - timedelta(days=60), january - timedelta(days=89)):
                watch.append_row(data, watch.build_row(moment, answers()))
            self.assertEqual(len(watch.load_history(data, january)), 3)


class Page(unittest.TestCase):
    """The page is one file. The stale rule runs in the reader's browser,
    so its test runs the page's own functions in Node when Node is here."""

    html = (ROOT / "index.html").read_text(encoding="utf-8")

    def run_pure(self, expression):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed: the page's functions were not run")
        match = re.search(r"/\* pure:start \*/(.*?)/\* pure:end \*/", self.html, re.S)
        self.assertIsNotNone(match)
        script = match.group(1) + "\nconsole.log(JSON.stringify(" + expression + "));"
        done = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, timeout=30, check=True
        )
        return json.loads(done.stdout)

    def test_the_summary_tells_the_page_the_stale_limit(self):
        summary = watch.summarize([row(0)], T0)
        self.assertEqual(summary["stale_after_minutes"], 20)
        self.assertIn("last.stale_after_minutes", self.html)
        self.assertIn("DEFAULT_STALE_MINUTES = 20", self.html)

    def test_the_stale_rule(self):
        minute = 60000
        got = self.run_pure(
            "[staleText(0, 20 * %d, 20), staleText(0, 20 * %d + 59999, 20),"
            " staleText(0, 21 * %d, 20), staleText(0, 47 * %d, 20),"
            " staleText(0, 180 * %d, 20), staleText(0, 3 * 1440 * %d, 20)]" % ((minute,) * 6)
        )
        self.assertEqual(
            got,
            [
                "",
                "",
                "The last check was 21 minutes ago. This page may be out of date.",
                "The last check was 47 minutes ago. This page may be out of date.",
                "The last check was 3 hours ago. This page may be out of date.",
                "The last check was 3 days ago. This page may be out of date.",
            ],
        )

    def test_times_are_us_central_twelve_hour(self):
        got = self.run_pure(
            "[centralTime('2026-11-12T23:05:02Z'), centralTime('2026-07-04T05:00:00Z'),"
            " centralTime('2026-11-12T18:00:00Z'), centralTime('nonsense')]"
        )
        self.assertEqual(
            got, ["11/12/2026 5:05 PM", "07/04/2026 12:00 AM", "11/12/2026 12:00 PM", ""]
        )

    def test_lengths_and_days_in_words(self):
        got = self.run_pure(
            "[durationText(1), durationText(35), durationText(125), durationText(1620),"
            " stripDay('2026-11-12', 0), stripDay('2026-11-12', 89),"
            " percentText(100), percentText(99.9)]"
        )
        self.assertEqual(
            got,
            [
                "1 minute",
                "35 minutes",
                "2 hours 5 minutes",
                "1 day 3 hours",
                "11/12/2026",
                "08/15/2026",
                "100%",
                "99.90%",
            ],
        )

    def test_the_page_loads_nothing_from_another_host(self):
        addresses = set(re.findall(r"https?://[^\s\"'<>)]+", self.html))
        self.assertEqual(
            {address.rstrip(".") for address in addresses},
            {"https://app.homedoctors4u.com", "https://www.homedoctors4u.com"},
        )
        self.assertNotRegex(self.html, r"<script[^>]+src=")
        self.assertNotRegex(self.html, r"<link[^>]+stylesheet")
        self.assertNotIn("@import", self.html)
        self.assertNotIn("innerHTML", self.html)
        self.assertEqual(re.findall(r"fetch\(([^,]+),", self.html), ['"data/summary.json?t=" + Date.now()'])

    def test_the_three_sentences_are_the_ones_of_the_page(self):
        self.assertEqual(
            watch.HEADLINES,
            {
                "ok": "All services are working.",
                "trouble": "Some services have trouble.",
                "down": "The website is not answering.",
            },
        )


if __name__ == "__main__":
    unittest.main()
