import sqlite3
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import test_health as baseline

from auth import set_password
from storage import get_db


class ReviewFixTests(unittest.TestCase):
    setUp = baseline.HealthTests.setUp
    get = baseline.HealthTests.get
    post = baseline.HealthTests.post
    login = baseline.HealthTests.login
    add_record = baseline.HealthTests.add_record

    def expire(self, seconds=1801):
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen=?", (int(time.time()) - seconds,)
                )

    def assert_no_records(self):
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM records").fetchone()[0], 0)

    def form(self, token, **extra):
        return {
            "csrf": token,
            "child": "合成测试",
            "occurred": "2026-11-01T10:30",
            "notes": "尚未保存的观察 <script>alert(1)</script>",
            **extra,
        }

    def test_expired_submission_retained_as_escaped_readonly_text(self):
        self.login()
        token = baseline.csrf(self.get("/record/new"))
        self.expire()
        response = self.post("/record/new", self.form(token))
        self.assertEqual(response.status_code, 400)
        body = response.get_data(as_text=True)
        self.assertIn("尚未保存的观察 &lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("readonly", body)
        self.assertNotIn('id="record-form"', body)
        self.assert_no_records()

    def test_rotated_csrf_preserves_input_without_saving(self):
        self.login()
        token = baseline.csrf(self.get("/record/new"))
        self.post("/logout", {"csrf": token})
        self.login()
        response = self.post("/record/new", self.form(token))
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未保存的观察", response.get_data(as_text=True))
        self.assert_no_records()

    def test_password_revocation_preserves_submission(self):
        self.login()
        token = baseline.csrf(self.get("/record/new"))
        with self.app.app_context():
            set_password("admin", "new-synthetic-password-12345")
        response = self.post("/record/new", self.form(token))
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未保存的观察", response.get_data(as_text=True))
        self.assert_no_records()

    def test_session_probe_requires_login_and_refreshes_rotated_token(self):
        response = self.get("/session-status")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("csrf", response.get_json())
        self.login()
        old_token = baseline.csrf(self.get("/record/new"))
        self.expire()
        self.assertEqual(self.get("/session-status").status_code, 401)
        self.login()
        state = self.get("/session-status")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.headers["Cache-Control"], "no-store")
        self.assertTrue(state.get_json()["authenticated"])
        token = state.get_json()["csrf"]
        self.assertNotEqual(token, old_token)
        self.assertEqual(self.post("/record/new", self.form(token)).status_code, 303)

    def test_null_fields_render_empty_and_save_empty(self):
        self.login()
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.execute("""INSERT INTO records(child,occurred,created,updated)
                    VALUES('合成测试','2026-11-01T10:30','fixture','fixture')""")
        response = self.get("/record/1/edit")
        text = response.get_data(as_text=True)
        self.assertNotIn('value="None"', text)
        self.assertNotIn(">None</textarea>", text)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT notes FROM records").fetchone()[0])
        self.assertEqual(
            self.post(
                "/record/1/edit", self.form(baseline.csrf(response), notes="", version="1")
            ).status_code,
            303,
        )
        with self.app.app_context():
            values = (
                get_db()
                .execute("SELECT temperature,symptoms,medication,care,notes FROM records")
                .fetchone()
            )
            self.assertEqual(tuple(values), ("", "", "", "", ""))

    def test_all_unicode_fields_at_declared_limit_are_accepted(self):
        self.login()
        token = baseline.csrf(self.get("/record/new"))
        values = {
            name: "😀" * 4000 for name in ["temperature", "symptoms", "medication", "care", "notes"]
        }
        response = self.post("/record/new", self.form(token, **values))
        self.assertEqual(response.status_code, 303)
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT notes FROM records").fetchone()[0], values["notes"]
            )

    def test_bad_host_rejected_before_database_or_body_for_get_and_post(self):
        with patch("auth.get_db", side_effect=AssertionError("Untrusted Host touched database")):
            for method, data in [("GET", None), ("POST", {}), ("POST", {"notes": "x" * 300000})]:
                response = self.client.open(
                    "/", base_url="https://evil.test", method=method, data=data
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_data(as_text=True), "Invalid host.")

    def test_locked_write_preserves_submission_without_partial_save(self):
        self.login()
        self.app.config["DATABASE_TIMEOUT"] = 0.01
        token = baseline.csrf(self.get("/record/new"))
        with closing(sqlite3.connect(self.path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.post("/record/new", self.form(token))
            lock.rollback()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "5")
        self.assertIn("尚未保存的观察", response.get_data(as_text=True))
        self.assert_no_records()

    def test_locked_refresh_does_not_block_authenticated_reads(self):
        self.login()
        self.expire(seconds=61)
        self.app.config["DATABASE_TIMEOUT"] = 0.01
        with closing(sqlite3.connect(self.path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertLogs(self.app.logger, level="WARNING"):
                response = self.get("/")
            lock.rollback()
        self.assertEqual(response.status_code, 200)

    def test_failed_expiry_cleanup_still_revokes_cookie(self):
        self.login()
        self.expire()
        self.app.config["DATABASE_TIMEOUT"] = 0.01
        with closing(sqlite3.connect(self.path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertLogs(self.app.logger, level="WARNING"):
                response = self.get("/session-status")
            lock.rollback()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.get("/login").status_code, 200)

    def test_logout_clears_cookie_when_session_delete_fails(self):
        self.login()
        token = baseline.csrf(self.get("/record/new"))
        self.app.config["DATABASE_TIMEOUT"] = 0.01
        with closing(sqlite3.connect(self.path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertLogs(self.app.logger, level="WARNING"):
                response = self.post("/logout", {"csrf": token})
            lock.rollback()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Clear-Site-Data"], '"cache", "cookies", "storage"')
        self.assertIsNone(self.client.get_cookie("__Host-health_session", domain=baseline.HOST))
        self.assertEqual(self.get("/session-status").status_code, 401)

    def test_missing_database_not_implicitly_created(self):
        self.path.rename(self.path.with_suffix(".saved"))
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.path.exists())

    def test_corrupt_database_returns_friendly_error(self):
        self.path.write_bytes(b"not a sqlite database")
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertIn("暂时无法读写记录", response.get_data(as_text=True))
        self.assertNotIn("Traceback", response.get_data(as_text=True))
