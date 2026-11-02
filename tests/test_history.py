"""History uses synthetic records only; querying must never change journal data."""

import html
import re
import unittest
from datetime import date
from urllib.parse import parse_qs, urlencode, urlparse

import test_health as baseline

from history import read_history
from storage import get_db


class HistoryTests(unittest.TestCase):
    setUp = baseline.HealthTests.setUp
    get = baseline.HealthTests.get
    post = baseline.HealthTests.post
    login = baseline.HealthTests.login

    def seed(self):
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.executemany(
                    "INSERT INTO family_members(id,name,relationship) VALUES(?,?,?)",
                    [(1, "合成小宝", "女儿"), (2, "合成妈妈", "母亲")],
                )
                connection.executemany(
                    """INSERT INTO records(id,member_id,child,occurred,temperature,symptoms,
                    medication,care,notes,created,updated) VALUES(?,?,?,?,?,?,?,?,?,'test','test')""",
                    [
                        (
                            1,
                            1,
                            "旧称呼",
                            "2026-11-02T10:30",
                            "38.2",
                            "合成咳嗽",
                            "",
                            "",
                            "概率100%_观察",
                        ),
                        (
                            2,
                            2,
                            "合成妈妈",
                            "2026-11-02T11:30",
                            "",
                            "",
                            "Amoxicillin",
                            "合成门诊",
                            "",
                        ),
                        (3, 1, "旧称呼", "2026-10-31T23:59", "", "旧月症状", "", "", ""),
                        (4, 2, "合成妈妈", "2024-02-29T09:00", "", "", "", "", "合成闰日"),
                        (5, 2, "合成妈妈", "2026-11-30T23:59", "", "", "", "", "月末记录"),
                    ],
                )
                for name in ["合成检验报告.pdf", "另一份合成检验报告.pdf"]:
                    content = b"%PDF-1.4\nBODY-ONLY-TOKEN\n%%EOF"
                    connection.execute(
                        "INSERT INTO attachments(record_id,filename,size,content,created) VALUES(2,?,?,?,?)",
                        (name, len(content), content, "test"),
                    )

    def history(self, **filters):
        return self.get("/history?" + urlencode(filters))

    def data(self, **args):
        with self.app.app_context():
            return read_history(get_db(), args, date(2026, 11, 2))

    def test_history_requires_login_and_header_entry_is_visible_after_login(self):
        self.assertEqual(self.get("/history").status_code, 302)
        self.assertNotIn('href="/history"', self.get("/login").get_data(as_text=True))
        self.login()
        self.assertIn('href="/history"', self.get().get_data(as_text=True))
        response = self.get("/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("历史记录", response.get_data(as_text=True))
        self.assertIn("日历查看", response.get_data(as_text=True))
        self.assertIn("列表查看", response.get_data(as_text=True))

    def test_search_spans_all_dates_and_every_documented_field(self):
        self.seed()
        expected = {
            "旧称呼": [1, 3],
            "女儿": [1, 3],
            "合成妈妈": [5, 2, 4],
            "38.2": [1],
            "合成咳嗽": [1],
            "amoxicillin": [2],
            "合成门诊": [2],
            "合成闰日": [4],
            "合成检验报告": [2],
            "BODY-ONLY-TOKEN": [],
            "no match": [],
        }
        for q, ids in expected.items():
            with self.subTest(q=q):
                state = self.data(view="list", q=q)
                self.assertEqual([r["id"] for r in state["records"]], ids)
                self.assertEqual(state["total"], len(ids))

    def test_percent_underscore_and_sql_are_literal_and_html_is_escaped(self):
        self.seed()
        self.login()
        for q, expected in [("100%_", [1]), ("%", [1]), ("_", [1]), ("' OR 1=1 --", [])]:
            self.assertEqual([r["id"] for r in self.data(view="list", q=q)["records"]], expected)
        body = self.history(view="list", q="<script>unsafe()</script>").get_data(as_text=True)
        self.assertIn("&lt;script&gt;unsafe()&lt;/script&gt;", body)
        self.assertNotIn("<script>unsafe()</script>", body)
        self.assertIn("没有找到记录", body)

    def test_family_date_and_keyword_filters_combine_inclusively(self):
        self.seed()
        state = self.data(view="list", member="1", start="2026-11-02", end="2026-11-02", q="观察")
        self.assertEqual([r["id"] for r in state["records"]], [1])
        self.assertEqual(
            [r["id"] for r in self.data(view="list", end="2026-10-31")["records"]], [3, 4]
        )
        self.assertEqual(
            [r["id"] for r in self.data(view="list", start="2026-11-30")["records"]], [5]
        )

    def test_calendar_default_month_counts_day_and_empty_day(self):
        self.seed()
        state = self.data()
        self.assertEqual(state["month"], "2026-11")
        self.assertEqual(state["month_total"], 3)
        self.assertEqual(state["total"], 3)
        self.assertEqual([r["id"] for r in state["records"]], [5, 2, 1])
        cells = {c["date"]: c for w in state["weeks"] for c in w if c["number"]}
        self.assertEqual(cells["2026-11-02"]["count"], 2)
        self.assertTrue(cells["2026-11-02"]["today"])
        self.assertEqual(cells["2026-11-30"]["count"], 1)
        day = self.data(day="2026-11-02")
        self.assertEqual(day["total"], 2)
        self.assertEqual(day["month_total"], 3)
        self.assertEqual(self.data(day="2026-11-03")["total"], 0)

    def test_calendar_counts_use_same_family_and_search_filters(self):
        self.seed()
        state = self.data(month="2026-11", member="2", q="合成检验报告")
        self.assertEqual(state["month_total"], 1)  # Two files still describe one record.
        self.assertEqual([r["id"] for r in state["records"]], [2])
        counts = {c["date"]: c["count"] for w in state["weeks"] for c in w if c["number"]}
        self.assertEqual(counts["2026-11-02"], 1)
        self.assertEqual(counts["2026-11-30"], 0)

    def test_search_calendar_opens_most_recent_matching_month(self):
        self.seed()
        self.assertEqual(self.data(q="合成闰日")["month"], "2024-02")
        self.assertEqual(self.data(q="旧月症状")["month"], "2026-10")
        self.assertEqual(self.data(q="不存在", start="2025-01-01")["month"], "2025-01")
        self.assertEqual(self.data(q="合成闰日", month="2026-11")["total"], 0)

    def test_leap_day_and_year_edges_have_correct_month_navigation(self):
        self.seed()
        state = self.data(month="2024-02", day="2024-02-29")
        self.assertEqual(state["total"], 1)
        cells = [c for week in state["weeks"] for c in week if c["number"]]
        self.assertEqual(len(cells), 29)
        self.assertEqual(self.data(month="2025-02")["month_total"], 0)
        self.assertEqual(self.data(month="2026-01")["previous_month"], "2025-12")
        self.assertEqual(self.data(month="2026-12")["next_month"], "2027-01")
        self.assertIsNone(self.data(month="0001-01")["previous_month"])
        self.assertIsNone(self.data(month="9999-12")["next_month"])

    def test_pagination_and_calendar_totals_cover_more_than_one_page(self):
        self.seed()
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.executemany(
                    """INSERT INTO records(member_id,child,occurred,notes,created,updated)
                    VALUES(1,'合成小宝','2026-11-15T12:00',?,'test','test')""",
                    [(f"分页+%_{i:03}",) for i in range(53)],
                )
        self.login()
        params = dict(
            view="calendar",
            month="2026-11",
            day="2026-11-15",
            member="1",
            q="分页+%_",
            start="2026-11-01",
            end="2026-11-30",
        )
        response = self.history(**params)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertEqual(body.count("<article"), 50)
        self.assertIn("本月 53 条", body)
        match = re.search(r'href="([^"]+)"[^>]*>下一页', body)
        self.assertIsNotNone(match)
        next_url = html.unescape(match.group(1))
        query = parse_qs(urlparse(next_url).query)
        for key, value in params.items():
            self.assertEqual(query[key], [value])
        self.assertEqual(query["page"], ["2"])
        second = self.get(next_url).get_data(as_text=True)
        self.assertEqual(second.count("<article"), 3)
        self.assertEqual(self.history(**params, page="3").status_code, 404)

    def test_invalid_filters_are_explained_and_input_is_preserved(self):
        self.seed()
        self.login()
        for args, message in [
            ({"start": "2026-02-30"}, "开始日期"),
            ({"end": "2026-2-01"}, "结束日期"),
            ({"start": "2026-11-03", "end": "2026-11-02"}, "不能晚于"),
            ({"member": "9223372036854775808"}, "所选家人"),
            ({"member": "1 OR 1=1"}, "所选家人"),
            ({"month": "2026-13"}, "月份"),
            ({"month": "2026-1"}, "月份"),
            ({"month": "0000-01"}, "月份"),
            ({"day": "2026-11-31"}, "所选日期"),
            ({"month": "2026-11", "day": "2026-10-31"}, "不一致"),
            ({"q": "x" * 201}, "200 字"),
            ({"page": "0"}, "页码"),
            ({"page": "-1"}, "页码"),
            ({"page": "1e3"}, "页码"),
            ({"view": "invalid"}, "日历或列表"),
        ]:
            with self.subTest(args=args):
                response = self.history(**args)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_data(as_text=True))
                self.assertNotIn("<article", response.get_data(as_text=True))
        body = self.history(start="bad", q="保留合成搜索").get_data(as_text=True)
        self.assertIn("保留合成搜索", body)

    def test_history_navigation_preserves_filters_and_clears_day_when_changing_month(self):
        self.seed()
        self.login()
        body = self.history(month="2026-11", day="2026-11-02", q="合成", member="1").get_data(
            as_text=True
        )
        previous = re.search(r'href="([^"]+)" aria-label="上个月"', body).group(1)
        query = parse_qs(urlparse(html.unescape(previous)).query)
        self.assertEqual(query["month"], ["2026-10"])
        self.assertEqual(query["q"], ["合成"])
        self.assertEqual(query["member"], ["1"])
        self.assertNotIn("day", query)
        self.assertIn("#history-results", body)

    def test_history_search_never_reads_attachment_content(self):
        import sqlite3

        self.seed()
        with self.app.app_context():
            connection = get_db()

            def authorize(action, table, column, *unused):
                if action == sqlite3.SQLITE_READ and table == "attachments" and column == "content":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            state = read_history(connection, {"q": "检验报告", "view": "list"}, date(2026, 11, 2))
            self.assertEqual(state["total"], 1)
            self.assertEqual(state["records"][0]["attachment_count"], 2)

    def test_queries_do_not_mutate_records_families_or_attachment_bytes(self):
        self.seed()
        self.login()

        def snapshot():
            with self.app.app_context():
                return {
                    table: [
                        tuple(row) for row in get_db().execute(f"SELECT * FROM {table} ORDER BY id")
                    ]
                    for table in ("records", "family_members", "attachments")
                }

        before = snapshot()
        for params in [
            {},
            {"view": "list"},
            {"q": "合成检验报告"},
            {"month": "2024-02"},
            {"month": "2026-11", "day": "2026-11-02"},
        ]:
            self.assertEqual(self.history(**params).status_code, 200)
        self.assertEqual(snapshot(), before)
