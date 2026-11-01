"""Private family health journal. Run schema changes explicitly with init-db."""

import argparse
import getpass
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
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
    url_for,
)
from werkzeug.exceptions import HTTPException, SecurityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from auth import (
    csrf,
    end_session,
    login_required,
    protect_request,
    reserve_login_attempt,
    set_password,
    start_session,
)
from storage import close_db, get_db, init_db

TIMEZONE = ZoneInfo("Australia/Melbourne")
FIELDS = ("child", "occurred", "temperature", "symptoms", "medication", "care", "notes")
PAGE_SIZE = 50


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
        MAX_FORM_PARTS=20,
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
            values = {key: request.form.get(key, "") for key in FIELDS}
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
            413: "提交内容过大，请缩短内容后重试。",
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
                """SELECT * FROM records
            ORDER BY occurred DESC, id DESC LIMIT ? OFFSET ?""",
                (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE),
            )
            .fetchall()
        )
        if page > 1 and not records:
            abort(404)
        return render_template(
            "index.html", records=records[:PAGE_SIZE], page=page, has_next=len(records) > PAGE_SIZE
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
        if request.method == "POST":
            values = {key: request.form.get(key, "").strip() for key in FIELDS}
            values["version"] = request.form.get("version", "")
            if not values["child"] or len(values["child"]) > 100:
                error = "请填写孩子称呼（不超过 100 字）。"
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
            if error:
                status = 400
            else:
                now = datetime.now(TIMEZONE).isoformat(timespec="microseconds")
                data = tuple(values[key] for key in FIELDS)
                connection = get_db()
                with connection:
                    if old:
                        result = connection.execute(
                            """UPDATE records SET child=?, occurred=?,
                            temperature=?, symptoms=?, medication=?, care=?, notes=?, updated=?,
                            version=version+1 WHERE id=? AND version=?""",
                            (*data, now, record_id, int(values["version"])),
                        )
                        conflict = result.rowcount == 0
                    else:
                        connection.execute(
                            """INSERT INTO records(child,occurred,temperature,
                            symptoms,medication,care,notes,created,updated)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                            (*data, now, now),
                        )
                if not conflict:
                    return redirect(url_for("index"), code=303)
                status = 409
                error = "此记录已被其他页面修改。你的输入尚未保存，请打开最新记录核对后再填写。"
        return render_template(
            "record.html",
            record=values,
            record_id=record_id,
            error=error,
            conflict=conflict,
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
