"""Private family health journal. Run schema changes explicitly with init-db."""

import argparse
import getpass
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.exceptions import HTTPException, SecurityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from attachments import MAX_RECORD_ATTACHMENTS, MAX_UPLOAD_REQUEST, read_uploads
from auth import (
    csrf,
    end_session,
    login_required,
    protect_request,
    reserve_login_attempt,
    set_password,
    start_session,
)
from history import HistoryInputError, member_colors_css, read_history
from storage import close_db, get_db, init_db

TIMEZONE = ZoneInfo("Australia/Melbourne")
FIELDS = ("child", "occurred", "temperature", "symptoms", "medication", "care", "notes")
PAGE_SIZE = 50
SYMPTOM_OPTIONS = (
    ("low_fever", "🌡️", "低烧"),
    ("fever_38", "🌡️", "发热 38°C"),
    ("fever_39", "🔥", "高烧 39°C"),
    ("vomiting", "🤮", "呕吐"),
    ("runny_nose", "🤧", "流鼻涕"),
    ("cough", "😷", "咳嗽"),
)


def submitted_symptoms(form):
    """Keep the existing text storage and accept forms opened before this update."""
    details = form.get("symptoms", "").strip()
    if form.get("symptom_picker") != "1":
        return details
    selected = set(form.getlist("symptom_choices"))
    summary = "、".join(label for key, _, label in SYMPTOM_OPTIONS if key in selected)
    return "\n".join(part for part in (summary, details) if part)


def split_symptoms(value):
    """Only recognize a whole first line of known labels; preserve all other text."""
    first, separator, details = value.partition("\n")
    labels = first.split("、")
    known = {label: key for key, _, label in SYMPTOM_OPTIONS}
    if labels and all(label in known for label in labels):
        return {known[label] for label in labels}, details if separator else ""
    return set(), value



def create_app(config=None):
    app = Flask(__name__)
    data = Path(os.environ.get("HEALTH_DATA", "/var/lib/child-health"))
    trusted_hosts = [
        host.strip()
        for host in os.environ.get("HEALTH_TRUSTED_HOSTS", "localhost,127.0.0.1").split(",")
        if host.strip()
    ]
    if not trusted_hosts:
        raise RuntimeError("HEALTH_TRUSTED_HOSTS must contain at least one hostname.")
    app.config.from_mapping(
        DATABASE=str(data / "health.sqlite3"),
        DATABASE_TIMEOUT=10,
        SECRET_KEY=None,
        TRUSTED_HOSTS=trusted_hosts,
        SESSION_COOKIE_NAME="__Host-health_session",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        # 20,000 Unicode characters may expand to 240 KB in an HTML form.
        MAX_CONTENT_LENGTH=256 * 1024,
        MAX_FORM_MEMORY_SIZE=256 * 1024,
        MAX_FORM_PARTS=30,
    )
    if config:
        app.config.update(config)
    if app.config["SECRET_KEY"] is None:
        app.config["SECRET_KEY"] = (data / "session.key").read_bytes()
    if len(app.secret_key) < 32:
        raise RuntimeError("Session signing key must contain at least 32 bytes.")
    # This application is reachable only on loopback behind exactly one Caddy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    # Perform the same expensive hash verification for nonexistent usernames.
    app.extensions["dummy_password"] = generate_password_hash(secrets.token_urlsafe(32))
    app.teardown_appcontext(close_db)

    @app.before_request
    def upload_limit():
        if (
            request.endpoint == "record"
            and request.method == "POST"
            and request.mimetype == "multipart/form-data"
        ):
            request.max_content_length = MAX_UPLOAD_REQUEST

    app.before_request(protect_request)
    app.jinja_env.globals["csrf"] = csrf

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; img-src 'self'; manifest-src 'self'; worker-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/manifest.webmanifest")
    def pwa_manifest():
        response = app.send_static_file("manifest.webmanifest")
        response.mimetype = "application/manifest+json"
        return response

    @app.get("/sw.js")
    def service_worker():
        response = app.send_static_file("sw.js")
        response.mimetype = "application/javascript"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    def preserve_submission(message):
        """Echo only this request's escaped fields; never save or auto-replay them."""
        if request.method != "POST" or request.endpoint != "record":
            return None
        try:
            values = {key: request.form.get(key, "") for key in (*FIELDS, "member_id")}
            values["symptoms"] = submitted_symptoms(request.form)
        except HTTPException:
            return None
        if not any(values.values()):
            return None
        return render_template("submission-recovery.html", values=values, message=message)

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def http_error(error):
        if isinstance(error, SecurityError):
            return "Invalid host.", 400, {"Content-Type": "text/plain; charset=utf-8"}
        messages = {
            400: "请求无法验证，请返回并刷新页面后重试。",
            404: "没有找到这个页面或记录。",
            413: "提交内容过大。病例单每个最多 10 MB，每次最多 3 个，请返回表单调整后重试。",
        }
        if error.code == 400:
            preserved = preserve_submission("页面验证未通过，提交内容尚未保存。")
            if preserved is not None:
                return preserved, 400
        return render_template("error.html", message=messages[error.code]), error.code

    @app.errorhandler(sqlite3.DatabaseError)
    def database_error(error):
        app.logger.exception("Database operation failed")
        message = "暂时无法读写记录。此次提交未确认保存，请先核对已有记录再操作。"
        body = preserve_submission(message)
        if body is None:
            body = render_template("error.html", message=message)
        response = make_response(body, 503)
        response.headers["Retry-After"] = "5"
        return response

    @app.get("/session-status")
    def session_status():
        if g.user is None:
            return jsonify(authenticated=False), 401
        return jsonify(authenticated=True, csrf=csrf())

    @app.get("/family-members")
    def family_options():
        if g.user is None:
            return jsonify(authenticated=False), 401
        members = (
            get_db()
            .execute("SELECT id,name,relationship FROM family_members ORDER BY id")
            .fetchall()
        )
        return jsonify(members=[dict(member) for member in members])

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user is not None:
            return redirect(url_for("index"))
        error = ""
        if request.method == "POST":
            retry_after = reserve_login_attempt()
            if retry_after:
                response = make_response(
                    render_template("login.html", error="当前网络尝试次数过多，请稍后重试。"), 429
                )
                response.headers["Retry-After"] = str(retry_after)
                return response
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = (
                get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
                if 0 < len(username) <= 100
                else None
            )
            password_hash = user["password"] if user else app.extensions["dummy_password"]
            valid = check_password_hash(password_hash, password) if len(password) <= 1024 else False
            if user and valid:
                start_session(user)
                return redirect(url_for("index"), code=303)
            error = "账号或密码不正确。"
        return render_template("login.html", error=error)

    @app.post("/logout")
    @login_required
    def logout():
        end_session()
        response = redirect(url_for("login"), code=303)
        response.headers["Clear-Site-Data"] = '"cache", "cookies", "storage"'
        return response

    @app.get("/")
    @login_required
    def index():
        page_text = request.args.get("page", "1")
        if not re.fullmatch(r"[0-9]{1,6}", page_text) or int(page_text) < 1:
            abort(400)
        page = int(page_text)
        records = (
            get_db()
            .execute(
                """SELECT r.*, COALESCE(m.name,r.child) AS member_name,
                (SELECT count(*) FROM attachments a WHERE a.record_id=r.id) AS attachment_count
                FROM records r LEFT JOIN family_members m ON m.id=r.member_id
                ORDER BY r.occurred DESC, r.id DESC LIMIT ? OFFSET ?""",
                (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
            )
            .fetchall()
        )
        if page > 1 and not records:
            abort(404)
        return render_template(
            "index.html", records=records[:PAGE_SIZE], page=page, has_next=len(records) > PAGE_SIZE
        )

    @app.get("/calendar-colors.css")
    @login_required
    def calendar_colors():
        ids = [row["id"] for row in get_db().execute("SELECT id FROM family_members ORDER BY id")]
        response = make_response(member_colors_css(ids))
        response.headers["Content-Type"] = "text/css; charset=utf-8"
        return response

    @app.get("/history")
    @login_required
    def history():
        try:
            context = read_history(get_db(), request.args, datetime.now(TIMEZONE).date(), PAGE_SIZE)
        except HistoryInputError as error:
            return render_template(
                "history.html",
                error=str(error),
                filters={key: request.args.get(key, "") for key in ("q", "member", "start", "end")},
                view="list",
                members=get_db()
                .execute("SELECT id,name FROM family_members ORDER BY id")
                .fetchall(),
            ), 400
        if context["page"] > context["pages"]:
            abort(404)

        def history_url(**changes):
            state = {
                **context["filters"],
                **{key: context[key] for key in ("view", "month", "day", "page")},
                **changes,
            }
            return url_for("history", **{key: value for key, value in state.items() if value})

        return render_template("history.html", history_url=history_url, error="", **context)

    @app.route("/settings", methods=["GET", "POST"])
    @app.route("/settings/member/<int:member_id>/edit", methods=["GET", "POST"])
    @login_required
    def settings(member_id=None):
        connection = get_db()
        old = (
            connection.execute("SELECT * FROM family_members WHERE id=?", (member_id,)).fetchone()
            if member_id is not None
            else None
        )
        if member_id is not None and old is None:
            abort(404)
        values = dict(old) if old else {}
        error, status = "", 200
        if request.method == "POST":
            values = {
                key: request.form.get(key, "").strip()
                for key in ("name", "relationship", "birthday", "notes", "version")
            }
            if not values["name"] or len(values["name"]) > 100:
                error = "请填写家人称呼（不超过 100 字）。"
            elif len(values["relationship"]) > 100 or len(values["notes"]) > 2000:
                error = "关系不超过 100 字，健康备注不超过 2000 字。"
            elif old and not re.fullmatch(r"[0-9]{1,10}", values["version"]):
                error = "版本信息缺失，请重新打开设置。"
            if values["birthday"]:
                try:
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", values["birthday"]):
                        raise ValueError
                    birthday = datetime.strptime(values["birthday"], "%Y-%m-%d").date()
                    if birthday > datetime.now(TIMEZONE).date():
                        raise ValueError
                except ValueError:
                    error = "请填写有效的出生日期，不能晚于今天。"
            if error:
                status = 400
            else:
                data = tuple(values[key] for key in ("name", "relationship", "birthday", "notes"))
                try:
                    with connection:
                        if old:
                            result = connection.execute(
                                """UPDATE family_members SET name=?,relationship=?,birthday=?,
                                notes=?,version=version+1 WHERE id=? AND version=?""",
                                (*data, member_id, int(values["version"])),
                            )
                            if result.rowcount == 0:
                                error, status = "家人信息已在其他页面修改，请重新打开核对。", 409
                        else:
                            connection.execute(
                                """INSERT INTO family_members(name,relationship,birthday,notes)
                                VALUES(?,?,?,?)""",
                                data,
                            )
                except sqlite3.IntegrityError:
                    error, status = "已有相同称呼的家人，请使用不同称呼。", 400
                if not error:
                    return redirect(url_for("settings", saved=1), code=303)
        members = connection.execute("SELECT * FROM family_members ORDER BY id").fetchall()
        return render_template(
            "settings.html",
            members=members,
            member=values,
            member_id=member_id,
            error=error,
            today=datetime.now(TIMEZONE).strftime("%Y-%m-%d"),
        ), status

    @app.get("/record/<int:record_id>/attachment/<int:attachment_id>")
    @login_required
    def attachment(record_id, attachment_id):
        file = (
            get_db()
            .execute(
                "SELECT filename,content FROM attachments WHERE id=? AND record_id=?",
                (attachment_id, record_id),
            )
            .fetchone()
        )
        if file is None:
            abort(404)
        return send_file(
            BytesIO(file["content"]),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=file["filename"],
            conditional=False,
            etag=False,
        )

    @app.route("/record/new", methods=["GET", "POST"])
    @app.route("/record/<int:record_id>/edit", methods=["GET", "POST"])
    @login_required
    def record(record_id=None):
        old = (
            get_db().execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record_id is not None
            else None
        )
        if record_id is not None and old is None:
            abort(404)
        status, error, conflict = 200, "", False
        values = (
            {key: "" if value is None else value for key, value in dict(old).items()}
            if old
            else None
        )
        members = get_db().execute("SELECT * FROM family_members ORDER BY id").fetchall()
        copy_mode = record_id is None and request.args.get("copy") is not None
        if copy_mode and request.method == "GET":
            copy_id = request.args.get("copy", "")
            if copy_id == "last":
                source = get_db().execute(
                    "SELECT * FROM records ORDER BY occurred DESC, id DESC LIMIT 1"
                ).fetchone()
            elif re.fullmatch(r"[1-9][0-9]{0,9}", copy_id):
                source = get_db().execute("SELECT * FROM records WHERE id=?", (int(copy_id),)).fetchone()
            else:
                abort(404)
            if source is None:
                abort(404)
            values = {key: source[key] or "" for key in FIELDS}
            values["member_id"] = source["member_id"] or ""
            values["occurred"] = datetime.now(TIMEZONE).strftime("%Y-%m-%dT%H:%M")
        uploads = []
        if request.method == "POST":
            values = {key: request.form.get(key, "").strip() for key in FIELDS}
            values["symptoms"] = submitted_symptoms(request.form)
            values["version"] = request.form.get("version", "")
            values["member_id"] = request.form.get("member_id", "")
            member = None
            if "member_id" in request.form:
                member = next((m for m in members if str(m["id"]) == values["member_id"]), None)
                if member:
                    values["child"] = member["name"]
                elif old and old["member_id"] is None and values["member_id"] == "legacy":
                    values["child"] = old["child"]
                else:
                    error = "请选择家人；还没有家人时，请先到设置中添加。"
            elif not values["child"] or len(values["child"]) > 100:
                # Compatibility for forms opened before this release.
                error = "请填写家人称呼（不超过 100 字）。"
            try:
                if not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", values["occurred"]
                ):
                    raise ValueError
                datetime.strptime(values["occurred"], "%Y-%m-%dT%H:%M")
            except ValueError:
                error = "请填写有效的记录时间。"
            if any(len(values[key]) > 4000 for key in FIELDS[2:]):
                error = "每项内容不能超过 4000 字。"
            if old and not re.fullmatch(r"[0-9]{1,10}", values["version"]):
                error = "版本信息缺失，请重新打开记录。"
            try:
                uploads = read_uploads(request.files.getlist("documents"))
            except ValueError as failure:
                error = str(failure)
            if error:
                status = 400
            else:
                now = datetime.now(TIMEZONE).isoformat(timespec="microseconds")
                data = tuple(values[key] for key in FIELDS)
                connection = get_db()
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if member is not None:
                        # Read again under the write lock, so a concurrent rename is consistent.
                        member = connection.execute(
                            "SELECT * FROM family_members WHERE id=?", (member["id"],)
                        ).fetchone()
                        values["child"] = member["name"]
                        data = tuple(values[key] for key in FIELDS)
                    elif "member_id" not in request.form:
                        connection.execute(
                            "INSERT OR IGNORE INTO family_members(name) VALUES(?)",
                            (values["child"],),
                        )
                        member = connection.execute(
                            "SELECT * FROM family_members WHERE name=?", (values["child"],)
                        ).fetchone()
                    selected_id = member["id"] if member else None
                    count = connection.execute(
                        "SELECT count(*) FROM attachments WHERE record_id=?", (record_id,)
                    ).fetchone()[0]
                    if count + len(uploads) > MAX_RECORD_ATTACHMENTS:
                        error, status = "每条记录最多保存 20 个病例单，请新建另一条记录。", 400
                    if old:
                        result = (
                            connection.execute(
                                """UPDATE records SET child=?, occurred=?,
                            temperature=?, symptoms=?, medication=?, care=?, notes=?, updated=?,
                            member_id=?, version=version+1 WHERE id=? AND version=?""",
                                (*data, now, selected_id, record_id, int(values["version"])),
                            )
                            if not error
                            else None
                        )
                        conflict = result is not None and result.rowcount == 0
                        saved_id = record_id
                    elif not error:
                        result = connection.execute(
                            """INSERT INTO records(child,occurred,temperature,
                            symptoms,medication,care,notes,created,updated,member_id)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (*data, now, now, selected_id),
                        )
                        saved_id = result.lastrowid
                    if not error and not conflict:
                        connection.executemany(
                            """INSERT INTO attachments(record_id,filename,size,content,created)
                            VALUES(?,?,?,?,?)""",
                            [
                                (saved_id, name, len(content), content, now)
                                for name, content in uploads
                            ],
                        )
                    else:
                        connection.rollback()
                if not conflict and not error:
                    return redirect(url_for("index"), code=303)
                if conflict:
                    status = 409
                    error = "此记录已被其他页面修改。你的输入尚未保存，请打开最新记录核对后再填写。"
            if any(file.filename for file in request.files.getlist("documents")):
                error += " 病例单尚未上传，请重新选择文件。"
        files = (
            get_db()
            .execute(
                "SELECT id,filename,size FROM attachments WHERE record_id=? ORDER BY id",
                (record_id,),
            )
            .fetchall()
            if old
            else []
        )
        selected_symptoms, symptom_details = split_symptoms(values["symptoms"] if values else "")
        return render_template(
            "record.html",
            copy_mode=copy_mode,
            has_records=bool(get_db().execute("SELECT 1 FROM records LIMIT 1").fetchone()),
            symptom_options=SYMPTOM_OPTIONS,
            selected_symptoms=selected_symptoms,
            symptom_details=symptom_details,
            record=values,
            record_id=record_id,
            error=error,
            conflict=conflict,
            members=members,
            attachments=files,
            legacy=bool(old and old["member_id"] is None),
            now=datetime.now(TIMEZONE).strftime("%Y-%m-%dT%H:%M"),
        ), status

    return app


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db")
    password_command = commands.add_parser("set-password")
    password_command.add_argument("username")
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        init_db()
        if args.command == "set-password":
            password = getpass.getpass("新密码（至少 14 个字符）: ")
            if password != getpass.getpass("再次输入密码: "):
                parser.exit(1, "两次密码不一致。\n")
            try:
                set_password(args.username, password)
            except ValueError as error:
                parser.exit(1, str(error) + "\n")
            print("账号密码已设置，原有登录已失效。")
        else:
            print("数据库结构已就绪。")


if __name__ == "__main__":
    main()
