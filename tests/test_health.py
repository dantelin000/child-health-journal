import re
import secrets
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app import create_app
from auth import LOGIN_ATTEMPTS, reserve_login_attempt, set_password
from backup import create_backup
from storage import get_db, init_db

HOST = "health.test"
BASE = f"https://{HOST}"
PASSWORD = "fixture-password-12345"


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.get_data(as_text=True)).group(1)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "health.sqlite3"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": secrets.token_bytes(32),
                "DATABASE": str(self.path),
                "TRUSTED_HOSTS": [HOST],
            }
        )
        with self.app.app_context():
            init_db()
            set_password("admin", PASSWORD)
        self.client = self.app.test_client()

    def get(self, path="/", client=None, **kwargs):
        return (client or self.client).get(path, base_url=BASE, **kwargs)

    def post(self, path, data, client=None, **kwargs):
        return (client or self.client).post(path, data=data, base_url=BASE, **kwargs)

    def login(self, client=None, ip="198.51.100.1", password=PASSWORD):
        token = csrf(self.get("/login", client))
        return self.post(
            "/login",
            {"csrf": token, "username": "admin", "password": password},
            client,
            environ_overrides={"REMOTE_ADDR": ip},
        )

    def add_record(self):
        token = csrf(self.get("/record/new"))
        return self.post(
            "/record/new",
            {
                "csrf": token,
                "child": "<小宝>",
                "occurred": "2026-11-01T10:30",
                "symptoms": "发热",
                "notes": "观察",
            },
        )

    def test_authentication_csrf_and_trusted_host(self):
        for route in ["/", "/record/new", "/record/1/edit"]:
            self.assertEqual(self.get(route).status_code, 302)
        self.assertEqual(self.post("/login", {}).status_code, 400)
        token = csrf(self.get("/login"))
        self.assertEqual(self.post("/login", {"csrf": "非 ASCII"}).status_code, 400)
        self.assertEqual(
            self.post(
                "/login", {"csrf": token, "username": "' OR 1=1 --", "password": "guess"}
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/", base_url="https://evil.test").status_code, 400)
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.post("/record/new", {}).status_code, 400)

    def test_secure_cookie_and_headers(self):
        response = self.get("/login")
        cookie = response.headers["Set-Cookie"]
        for attribute in ["__Host-health_session=", "Secure", "HttpOnly", "SameSite=Lax", "Path=/"]:
            self.assertIn(attribute, cookie)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_create_edit_and_escaping(self):
        self.login()
        self.assertEqual(self.add_record().status_code, 303)
        text = self.get().get_data(as_text=True)
        self.assertIn("&lt;小宝&gt;", text)
        self.assertNotIn("<小宝>", text)
        response = self.get("/record/1/edit")
        self.assertIn('value="1"', response.get_data(as_text=True))
        response = self.post(
            "/record/1/edit",
            {
                "csrf": csrf(response),
                "version": "1",
                "child": "小宝",
                "occurred": "2026-11-01T11:30",
                "symptoms": "好转",
            },
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("好转", self.get().get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT version FROM records").fetchone()[0], 2)

    def test_edit_conflict_keeps_first_save_and_second_form(self):
        self.login()
        self.add_record()
        response = self.get("/record/1/edit")
        token = csrf(response)
        form = {"csrf": token, "version": "1", "child": "小宝", "occurred": "2026-11-01T11:30"}
        self.assertEqual(self.post("/record/1/edit", {**form, "notes": "先保存"}).status_code, 303)
        response = self.post("/record/1/edit", {**form, "notes": "另一个页面尚未保存的输入"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("另一个页面尚未保存的输入", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT notes FROM records").fetchone()[0], "先保存")

    def test_validation_and_size_limits(self):
        self.login()
        token = csrf(self.get("/record/new"))
        for date in ["2026-1-01T10:30", "2026-02-30T10:30", "2026-11-01T25:00"]:
            response = self.post(
                "/record/new",
                {"csrf": token, "child": "小宝", "occurred": date, "notes": "保留此内容"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("保留此内容", response.get_data(as_text=True))
        self.assertEqual(
            self.post(
                "/record/new", {"csrf": token, "child": "x" * 101, "occurred": "2026-11-01T10:30"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(
                "/record/new",
                {
                    "csrf": token,
                    "child": "小宝",
                    "occurred": "2026-11-01T10:30",
                    "notes": "x" * 4001,
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post("/record/new", {"csrf": token, "notes": "x" * 300000}).status_code, 413
        )
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT count(*) FROM records").fetchone()[0], 0)

    def test_logout_revokes_replayed_cookie(self):
        self.login()
        cookie = self.client.get_cookie("__Host-health_session", domain=HOST).value
        response = self.post("/logout", {"csrf": csrf(self.get("/record/new"))})
        self.assertEqual(response.status_code, 303)
        replay = self.app.test_client()
        replay.set_cookie("__Host-health_session", cookie, domain=HOST, secure=True)
        self.assertEqual(self.get(client=replay).status_code, 302)

    def test_password_change_revokes_all_sessions(self):
        self.login()
        second = self.app.test_client()
        self.assertEqual(self.login(second).status_code, 303)
        with self.app.app_context():
            set_password("admin", "new-fixture-password-123")
        self.assertEqual(self.get().status_code, 302)
        self.assertEqual(self.get(client=second).status_code, 302)
        self.assertEqual(self.login(password="new-fixture-password-123").status_code, 303)

    def test_session_idle_and_absolute_expiry(self):
        for column, age in [("last_seen", 1801), ("created_at", 43201)]:
            self.login()
            with self.app.app_context():
                connection = get_db()
                with connection:
                    connection.execute(
                        f"UPDATE auth_sessions SET {column}=?", (int(time.time()) - age,)
                    )
            self.assertEqual(self.get().status_code, 302)

    def test_password_not_silently_truncated(self):
        with self.app.app_context():
            set_password("admin", "x" * 1024)
        self.assertEqual(self.login(password="x" * 1024 + "y").status_code, 200)
        self.assertEqual(self.login(password="x" * 1024).status_code, 303)

    def test_rate_limit_is_per_client_and_expiring(self):
        for _ in range(LOGIN_ATTEMPTS):
            self.assertEqual(self.login(password="wrong").status_code, 200)
        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["Retry-After"]), 0)
        self.assertEqual(self.login(ip="198.51.100.2").status_code, 303)
        with self.app.app_context():
            connection = get_db()
            row = connection.execute("SELECT client_hash FROM login_limits").fetchone()
            self.assertNotIn("198.51.100.", row[0])
            with connection:
                connection.execute("UPDATE login_limits SET window_start=0")
        self.post("/logout", {"csrf": csrf(self.get("/record/new"))})
        self.assertEqual(self.login().status_code, 303)

    def test_rate_limit_reservation_is_atomic(self):
        def reserve(_):
            with self.app.test_request_context(
                "/login", environ_base={"REMOTE_ADDR": "198.51.100.3"}
            ):
                return reserve_login_attempt()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(results.count(0), LOGIN_ATTEMPTS)

    def test_proxy_uses_rightmost_client_address(self):
        token = csrf(self.get("/login"))
        for _ in range(LOGIN_ATTEMPTS):
            self.post(
                "/login",
                {"csrf": token, "username": "admin", "password": "wrong"},
                headers={"X-Forwarded-For": "198.51.100.40, 198.51.100.9"},
            )
        response = self.post(
            "/login",
            {"csrf": token, "username": "admin", "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.41, 198.51.100.9"},
        )
        self.assertEqual(response.status_code, 429)

    def test_all_records_accessible_with_pagination(self):
        with self.app.app_context():
            connection = get_db()
            with connection:
                connection.executemany(
                    """INSERT INTO records(child,occurred,created,updated)
                    VALUES(?,'2026-11-01T10:30','fixture','fixture')""",
                    [(f"child-{i:04}",) for i in range(1, 502)],
                )
        self.login()
        first = self.get().get_data(as_text=True)
        self.assertEqual(first.count("<article"), 50)
        self.assertIn("child-0501", first)
        last = self.get("/?page=11").get_data(as_text=True)
        self.assertIn("child-0001", last)
        self.assertEqual(last.count("<article"), 1)
        for query in ["0", "-1", "abc", "1e3"]:
            self.assertEqual(self.get("/?page=" + query).status_code, 400)
        self.assertEqual(self.get("/?page=12").status_code, 404)

    def test_backup_does_not_overwrite_and_can_restore(self):
        self.login()
        self.add_record()
        directory = Path(self.temp.name) / "backups"
        with patch("backup.datetime") as clock:
            clock.now.return_value.strftime.return_value = "same-second"
            first = create_backup(self.path, directory)
            second = create_backup(self.path, directory)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        with closing(sqlite3.connect(first)) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                restored.execute("SELECT child,notes FROM records").fetchone(), ("<小宝>", "观察")
            )
        self.assertFalse(list(directory.glob("*.partial")))

    def test_failed_backup_leaves_no_published_snapshot(self):
        corrupt = Path(self.temp.name) / "corrupt.sqlite3"
        corrupt.write_text("this is not a database")
        directory = Path(self.temp.name) / "failed-backups"
        with self.assertRaises(sqlite3.DatabaseError):
            create_backup(corrupt, directory)
        self.assertEqual(list(directory.iterdir()), [])
        with patch("backup.time.monotonic", side_effect=[0, 61]), self.assertRaises(TimeoutError):
            create_backup(self.path, directory)
        self.assertEqual(list(directory.iterdir()), [])


class MigrationTests(unittest.TestCase):
    def test_original_schema_migration_preserves_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript("""
                    CREATE TABLE users(username TEXT PRIMARY KEY,password TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE records(id INTEGER PRIMARY KEY,child TEXT NOT NULL,occurred TEXT NOT NULL,temperature TEXT,symptoms TEXT,medication TEXT,care TEXT,notes TEXT,created TEXT NOT NULL,updated TEXT NOT NULL);
                    CREATE TABLE login_attempts(id INTEGER PRIMARY KEY,at INTEGER NOT NULL);
                    INSERT INTO users VALUES('existing','unchanged-hash',5);
                    INSERT INTO records VALUES(1,'original-child','2026-10-01T12:00','38','symptom','medicine','care','note','created','updated');
                """)
                before = connection.execute("SELECT * FROM records").fetchall()
            app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": secrets.token_bytes(32),
                    "DATABASE": str(database),
                    "TRUSTED_HOSTS": [HOST],
                }
            )
            with app.app_context():
                init_db()
                init_db()  # Migration must be safe to repeat during restarts.
                connection = get_db()
                self.assertEqual(
                    tuple(connection.execute("SELECT * FROM users").fetchone()),
                    ("existing", "unchanged-hash", 5),
                )
                after = tuple(connection.execute("SELECT * FROM records").fetchone())
                self.assertEqual(after[: len(before[0])], before[0])
                self.assertEqual(after[-2], 1)
                self.assertIsNotNone(after[-1])


if __name__ == "__main__":
    unittest.main()
