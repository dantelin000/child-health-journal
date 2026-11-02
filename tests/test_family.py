"""Family and medical-document workflows use only synthetic records and files."""

import io
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_health as baseline

from backup import create_backup
from storage import get_db, init_db

PDF = b"%PDF-1.4\n% synthetic test document\n%%EOF\n"
PNG = (Path(__file__).resolve().parents[1] / "static/icons/apple-touch-icon.png").read_bytes()


class FamilyTests(unittest.TestCase):
    setUp = baseline.HealthTests.setUp
    get = baseline.HealthTests.get
    login = baseline.HealthTests.login

    def post(self, path, data, client=None, **kwargs):
        response = baseline.HealthTests.post(self, path, data, client, **kwargs)
        # Werkzeug's synthetic multipart encoder can spool the WSGI input to
        # disk. In production the WSGI server owns it; this fixture must close it.
        self.addCleanup(response.request.environ["wsgi.input"].close)
        return response

    def member(self, name="合成家人", **extra):
        response = self.post(
            "/settings",
            {
                "csrf": baseline.csrf(self.get("/settings")),
                "name": name,
                "relationship": "本人",
                "birthday": "1990-02-03",
                "notes": "合成健康备注 <script>unsafe()</script>",
                **extra,
            },
        )
        self.assertEqual(response.status_code, 303)
        return self.get("/family-members").get_json()["members"][-1]["id"]

    def form(self, **extra):
        return {
            "csrf": baseline.csrf(self.get("/record/new")),
            "member_id": "1",
            "occurred": "2026-11-02T10:30",
            "symptoms": "合成症状",
            **extra,
        }

    def counts(self):
        with self.app.app_context():
            return tuple(
                get_db().execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("family_members", "records", "attachments")
            )

    def test_private_settings_and_file_endpoints(self):
        for route in ["/settings", "/settings/member/1/edit", "/record/1/attachment/1"]:
            self.assertEqual(self.get(route).status_code, 302)
        self.assertEqual(self.get("/family-members").status_code, 401)
        self.login()
        self.assertEqual(self.post("/settings", {"name": "forged"}).status_code, 400)
        self.assertEqual(self.get("/record/1/attachment/1").status_code, 404)

    def test_family_presets_dropdown_and_rename_keep_record_link(self):
        self.login()
        member = self.member()
        html = self.get("/settings").get_data(as_text=True)
        self.assertIn("1990-02-03", html)
        self.assertIn("&lt;script&gt;unsafe()&lt;/script&gt;", html)
        self.assertIn('value="1"', self.get("/record/new").get_data(as_text=True))
        self.assertEqual(self.post("/record/new", self.form()).status_code, 303)
        self.assertEqual(
            self.post(
                f"/settings/member/{member}/edit",
                {
                    "csrf": baseline.csrf(self.get("/settings")),
                    "version": "1",
                    "name": "新的称呼",
                    "birthday": "",
                    "relationship": "父亲",
                    "notes": "",
                },
            ).status_code,
            303,
        )
        self.assertIn("新的称呼", self.get().get_data(as_text=True))
        with self.app.app_context():
            row = get_db().execute("SELECT child,member_id FROM records").fetchone()
            self.assertEqual(tuple(row), ("合成家人", member))
        self.assertIn('value="1" selected', self.get("/record/1/edit").get_data(as_text=True))

    def test_family_validation_and_concurrent_edit(self):
        self.login()
        self.member()
        token = baseline.csrf(self.get("/settings"))
        for form in [
            {"name": ""},
            {"name": "x" * 101},
            {"name": "合成家人"},
            {"name": "other", "birthday": "2099-01-01"},
            {"name": "other", "birthday": "2026-02-30"},
            {"name": "other", "notes": "x" * 2001},
        ]:
            self.assertEqual(self.post("/settings", {"csrf": token, **form}).status_code, 400)
        data = {"csrf": token, "name": "改称呼", "version": "1"}
        self.assertEqual(self.post("/settings/member/1/edit", data).status_code, 303)
        self.assertEqual(self.post("/settings/member/1/edit", data).status_code, 409)
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_invalid_or_empty_member_never_saves(self):
        self.login()
        self.member()
        for value in ["", "9999", "1 OR 1=1", "legacy"]:
            response = self.post("/record/new", self.form(member_id=value))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_upload_download_and_append_are_persistent_and_private(self):
        self.login()
        self.member()
        response = self.post(
            "/record/new",
            self.form(
                documents=[
                    (io.BytesIO(PDF), "../../病例单.pdf"),
                    (io.BytesIO(PNG), "报告.png"),
                ]
            ),
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.counts(), (1, 1, 2))
        self.assertIn("2 个病例单", self.get().get_data(as_text=True))
        body = self.get("/record/1/edit").get_data(as_text=True)
        self.assertIn("病例单.pdf", body)
        self.assertNotIn("../../", body)
        response = self.get("/record/1/attachment/1")
        self.addCleanup(response.close)
        self.assertEqual(response.data, PDF)
        self.assertEqual(response.mimetype, "application/octet-stream")
        self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn("filename*=", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.get("/record/2/attachment/1").status_code, 404)
        self.assertEqual(
            self.get("/record/1/attachment/1", client=self.app.test_client()).status_code, 302
        )
        self.assertEqual(
            self.post(
                "/record/1/edit", self.form(version="1", documents=(io.BytesIO(PDF), "补充.pdf"))
            ).status_code,
            303,
        )
        self.assertEqual(self.counts(), (1, 1, 3))
        self.assertEqual(self.get("/record/1/attachment/1").data, PDF)

    def test_empty_wrong_type_and_mismatched_uploads_do_not_save_text(self):
        self.login()
        self.member()
        for content, name in [
            (b"", "empty.pdf"),
            (b"<script>unsafe()</script>", "fake.pdf"),
            (PNG, "wrong.pdf"),
            (PDF, "report.html"),
            (PDF, "report.svg"),
        ]:
            response = self.post(
                "/record/new",
                self.form(notes="保留这段合成文字", documents=(io.BytesIO(content), name)),
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("保留这段合成文字", response.get_data(as_text=True))
            self.assertIn("重新选择文件", response.get_data(as_text=True))
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_upload_count_and_size_limits(self):
        self.login()
        self.member()
        with patch("attachments.MAX_FILE_BYTES", 16):
            response = self.post("/record/new", self.form(documents=(io.BytesIO(PDF), "big.pdf")))
        self.assertEqual(response.status_code, 400)
        response = self.post(
            "/record/new",
            self.form(documents=[(io.BytesIO(PDF), f"{number}.pdf") for number in range(4)]),
        )
        self.assertEqual(response.status_code, 400)
        with patch("app.MAX_UPLOAD_REQUEST", 1024):
            response = self.post(
                "/record/new",
                self.form(documents=(io.BytesIO(b"%PDF-1.4\n" + b"0" * 2000), "request.pdf")),
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_record_attachment_limit_rolls_back_edit(self):
        self.login()
        self.member()
        self.assertEqual(
            self.post("/record/new", self.form(documents=(io.BytesIO(PDF), "a.pdf"))).status_code,
            303,
        )
        with patch("app.MAX_RECORD_ATTACHMENTS", 1):
            response = self.post(
                "/record/1/edit",
                self.form(version="1", notes="不应保存", documents=(io.BytesIO(PDF), "b.pdf")),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.counts(), (1, 1, 1))
        with self.app.app_context():
            self.assertEqual(
                tuple(get_db().execute("SELECT version,notes FROM records").fetchone()), (1, "")
            )

    def test_stale_edit_cannot_add_orphaned_attachment(self):
        self.login()
        self.member()
        self.post("/record/new", self.form())
        self.assertEqual(self.post("/record/1/edit", self.form(version="1")).status_code, 303)
        response = self.post(
            "/record/1/edit", self.form(version="1", documents=(io.BytesIO(PDF), "stale.pdf"))
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.counts(), (1, 1, 0))

    def test_attachment_insert_failure_rolls_back_record_and_preserves_text(self):
        self.login()
        self.member()
        with self.app.app_context():
            connection = get_db()
            connection.execute("""CREATE TRIGGER synthetic_failure BEFORE INSERT ON attachments
                BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
            connection.commit()
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.post(
                "/record/new",
                self.form(notes="文字不应部分保存", documents=(io.BytesIO(PDF), "atomic.pdf")),
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("文字不应部分保存", response.get_data(as_text=True))
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_expired_session_does_not_save_uploaded_document(self):
        self.login()
        self.member()
        form = self.form(documents=(io.BytesIO(PDF), "expired.pdf"))
        with self.app.app_context():
            connection = get_db()
            connection.execute("UPDATE auth_sessions SET last_seen=0")
            connection.commit()
        response = self.post("/record/new", form)
        self.assertEqual(response.status_code, 400)
        self.assertIn("重新选择文件", response.get_data(as_text=True))
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_backup_restores_members_documents_and_original_bytes(self):
        self.login()
        self.member()
        self.post("/record/new", self.form(documents=(io.BytesIO(PDF), "backup.pdf")))
        backup = create_backup(self.path, Path(self.temp.name) / "backups")
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIsNone(connection.execute("PRAGMA foreign_key_check").fetchone())
            self.assertEqual(
                connection.execute("SELECT name FROM family_members").fetchone()[0], "合成家人"
            )
            self.assertEqual(
                connection.execute("SELECT content FROM attachments").fetchone()[0], PDF
            )
            self.assertEqual(connection.execute("SELECT member_id FROM records").fetchone()[0], 1)

    def test_large_multipart_file_does_not_use_small_text_form_limit(self):
        self.login()
        self.member()
        content = b"%PDF-1.4\n" + b"0" * (600 * 1024) + b"\n%%EOF"
        response = self.post("/record/new", self.form(documents=(io.BytesIO(content), "large.pdf")))
        self.assertEqual(response.status_code, 303)
        response = self.get("/record/1/attachment/1")
        self.addCleanup(response.close)
        self.assertEqual(response.data, content)

    def test_jpeg_webp_and_empty_picker(self):
        self.login()
        self.member()
        response = self.post(
            "/record/new",
            self.form(
                documents=[
                    (io.BytesIO(b"\xff\xd8\xff" + b"synthetic"), "photo.jpeg"),
                    (io.BytesIO(b"RIFF" + b"1234" + b"WEBP" + b"synthetic"), "photo.webp"),
                    (io.BytesIO(b""), ""),
                ]
            ),
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.counts(), (1, 1, 2))

    def test_migration_runs_once_and_keeps_every_existing_column(self):
        with self.app.app_context():
            connection = get_db()
            # Simulate the pre-family schema using synthetic data only.
            connection.executescript("""DROP TABLE attachments; DROP TABLE records;
                DROP TABLE family_members;
                CREATE TABLE records(id INTEGER PRIMARY KEY,child TEXT NOT NULL,
                    occurred TEXT NOT NULL,temperature TEXT,symptoms TEXT,medication TEXT,
                    care TEXT,notes TEXT,created TEXT NOT NULL,updated TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1);
                INSERT INTO records(child,occurred,created,updated,version)
                    VALUES('原家人','2026-10-31T10:30','created','updated',7),
                    ('原家人','2026-11-01T10:30','created','updated',2);
            """)
            before = [tuple(row) for row in connection.execute("SELECT * FROM records ORDER BY id")]
            init_db()
            init_db()
            after = [tuple(row) for row in connection.execute("SELECT * FROM records ORDER BY id")]
            self.assertEqual([row[:-1] for row in after], before)
            self.assertEqual([row[-1] for row in after], [1, 1])
            self.assertEqual(
                connection.execute("SELECT count(*) FROM family_members").fetchone()[0], 1
            )
            connection.execute("UPDATE family_members SET name='新称呼' WHERE id=1")
            connection.commit()
            init_db()
            self.assertEqual(
                connection.execute("SELECT count(*) FROM family_members").fetchone()[0], 1
            )
