"""Family event previews and authenticated, generated calendar colors."""

import re
import sqlite3
import unittest
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

import test_health as baseline
import test_history as history_baseline

from history import member_colors_css, read_history, record_summary
from storage import get_db


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.nested = False
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.nested |= self.depth > 0
            self.depth += 1
            self.links.append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == "a":
            self.depth -= 1


class CalendarDesignTests(unittest.TestCase):
    setUp = baseline.HealthTests.setUp
    get = baseline.HealthTests.get
    post = baseline.HealthTests.post
    login = baseline.HealthTests.login
    seed = history_baseline.HistoryTests.seed
    data = history_baseline.HistoryTests.data
    history = history_baseline.HistoryTests.history

    def cell(self, state, day="2026-11-02"):
        return next(cell for week in state["weeks"] for cell in week if cell["date"] == day)

    def test_colors_require_login_and_css_contains_no_family_text(self):
        self.seed()
        self.assertEqual(self.get("/calendar-colors.css").status_code, 302)
        self.login()
        response = self.get("/calendar-colors.css")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/css")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        css = response.get_data(as_text=True)
        self.assertIn(".member-color-1{", css)
        self.assertIn(".member-color-2{", css)
        self.assertNotIn("合成", css)
        body = self.get("/history").get_data(as_text=True)
        self.assertIn('href="/calendar-colors.css"', body)
        self.assertNotIn('style="', body)
        self.assertNotIn("'unsafe-inline'", response.headers["Content-Security-Policy"])

    def test_color_identity_is_stable_after_rename_filter_or_new_member(self):
        self.seed()
        self.login()
        first = self.get("/calendar-colors.css").get_data(as_text=True).splitlines()
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.execute("UPDATE family_members SET name='更新称呼' WHERE id=1")
                connection.execute("INSERT INTO family_members(name) VALUES('合成爸爸')")
        self.get("/history?member=1&month=2026-11")
        second = self.get("/calendar-colors.css").get_data(as_text=True).splitlines()
        self.assertEqual(second[:2], first)
        self.assertEqual(
            self.cell(self.data(month="2026-11"))["events"][0]["color_class"], "member-color-1"
        )
        self.assertEqual(member_colors_css([2, 1]), member_colors_css([1, 2]))

    def test_palette_has_distinct_accents_and_readable_text(self):
        css = member_colors_css(range(1, 25))
        rules = re.findall(
            r"--member-color:(#[0-9a-f]{6});--member-bg:(#[0-9a-f]{6});--member-text:(#[0-9a-f]{6});",
            css,
        )
        self.assertEqual(len(rules), 24)
        self.assertEqual(len({accent for accent, _, _ in rules}), 24)

        def luminance(color):
            channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
            return sum(
                v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
            )

        for _, background, text in rules:
            ratio = (luminance(background) + 0.05) / (luminance(text) + 0.05)
            self.assertGreaterEqual(ratio, 4.5)

    def test_previews_show_time_family_and_short_summary_in_chronological_order(self):
        self.seed()
        cell = self.cell(self.data(month="2026-11"))
        self.assertEqual([event["id"] for event in cell["events"]], [1, 2])
        self.assertEqual(cell["events"][0]["time"], "10:30")
        self.assertEqual(cell["events"][0]["name"], "合成小宝")
        self.assertEqual(cell["events"][0]["summary"], "合成咳嗽")
        self.assertEqual(cell["events"][1]["summary"], "合成门诊")
        self.assertNotEqual(cell["events"][0]["color_class"], cell["events"][1]["color_class"])
        self.assertEqual(sum(mark["count"] for mark in cell["family_marks"]), cell["count"])

    def test_preview_limit_and_hidden_family_markers_do_not_depend_on_pagination(self):
        self.seed()
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.execute("INSERT INTO family_members(id,name) VALUES(3,'合成爸爸')")
                connection.executemany(
                    """INSERT INTO records(member_id,child,occurred,symptoms,created,updated)
                    VALUES(1,'合成小宝','2026-11-02T12:00',?,'test','test')""",
                    [(f"合成观察{i}",) for i in range(8)],
                )
                connection.execute("""INSERT INTO records(member_id,child,occurred,created,updated)
                    VALUES(3,'合成爸爸','2026-11-02T23:00','test','test')""")
            state = read_history(connection, {"month": "2026-11"}, date(2026, 11, 2), page_size=1)
        cell = self.cell(state)
        self.assertEqual(cell["count"], 11)
        self.assertEqual(len(cell["events"]), 3)
        self.assertEqual(cell["more"], 8)
        self.assertEqual(cell["more_mobile"], 9)
        self.assertEqual(len(state["records"]), 1)
        self.assertIn("member-color-3", [m["color_class"] for m in cell["family_marks"]])
        self.assertNotIn("member-color-3", [event["color_class"] for event in cell["events"]])
        self.assertEqual(self.cell(self.data(month="2026-11", page="2"))["events"], cell["events"])

    def test_calendar_previews_and_markers_obey_the_search_and_family_filters(self):
        self.seed()
        cell = self.cell(self.data(month="2026-11", q="Amoxicillin", member="2"))
        self.assertEqual([event["id"] for event in cell["events"]], [2])
        self.assertEqual([mark["color_class"] for mark in cell["family_marks"]], ["member-color-2"])
        empty = self.cell(self.data(month="2026-11", start="2026-11-30"))
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["family_marks"], [])

    def test_calendar_previews_never_read_attachment_blobs(self):
        self.seed()
        with self.app.app_context():
            connection = get_db()

            def authorize(action, table, column, *unused):
                if action == sqlite3.SQLITE_READ and table == "attachments" and column == "content":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            state = read_history(
                connection, {"month": "2026-11", "q": "检验报告"}, date(2026, 11, 2)
            )
            self.assertEqual(self.cell(state)["events"][0]["id"], 2)

    def test_escaped_previews_link_to_record_and_legend_preserves_filters(self):
        self.seed()
        self.login()
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.execute(
                    "UPDATE family_members SET name=? WHERE id=1", ("<b>合成家人</b>",)
                )
                connection.execute(
                    "UPDATE records SET symptoms=? WHERE id=1", ("<script>合成内容</script>",)
                )
        response = self.history(month="2026-11", q="合成", start="2026-11-01")
        body = response.get_data(as_text=True)
        self.assertIn("&lt;script&gt;合成内容&lt;/script&gt;", body)
        self.assertNotIn("<script>合成内容</script>", body)
        self.assertNotIn("<b>合成家人</b>", body)
        parser = LinkParser()
        parser.feed(body)
        self.assertFalse(parser.nested)
        events = [link for link in parser.links if "calendar-event " in link.get("class", "")]
        self.assertTrue(any(link["href"] == "/record/1/edit" for link in events))
        legend = next(
            link for link in parser.links if link.get("class") == "legend-member member-color-1"
        )
        query = parse_qs(urlparse(legend["href"]).query)
        self.assertEqual(query["member"], ["1"])
        self.assertEqual(query["q"], ["合成"])
        self.assertEqual(query["start"], ["2026-11-01"])
        self.assertNotIn("day", query)

    def test_summary_fallbacks_normalize_whitespace_and_bound_text(self):
        row = dict(
            symptoms=None, care="", medication="", temperature="", notes="", attachment_count=0
        )
        self.assertEqual(record_summary(row), "健康记录")
        self.assertEqual(record_summary({**row, "attachment_count": 2}), "病例单 2 份")
        self.assertEqual(
            record_summary({**row, "medication": "  合成药\n  每日一次 "}), "用药：合成药 每日一次"
        )
        summary = record_summary({**row, "symptoms": "合成" * 100})
        self.assertEqual(len(summary), 60)
        self.assertTrue(summary.endswith("…"))
