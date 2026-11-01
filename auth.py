"""Revocable sessions and per-client login throttling for one family."""

import functools
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import time

from flask import abort, current_app, g, redirect, request, session, url_for
from werkzeug.exceptions import SecurityError
from werkzeug.security import generate_password_hash

from storage import get_db

IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 12 * 60 * 60
LOGIN_WINDOW = 15 * 60
LOGIN_ATTEMPTS = 10


def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def protect_request():
    g.user = None
    # Reject an invalid Host before touching the session or parsing its form.
    # Flask has no URL adapter for this routing error, including on POST.
    if isinstance(request.routing_exception, SecurityError):
        raise request.routing_exception
    if request.endpoint in {"static", "pwa_manifest", "service_worker"}:
        return
    now = int(time.time())
    sid = session.get("sid")
    if sid:
        connection = get_db()
        user = connection.execute(
            """SELECT u.username, u.generation, s.created_at, s.last_seen
            FROM auth_sessions s JOIN users u ON u.username=s.username
            WHERE s.token_hash=? AND s.generation=u.generation""",
            (token_hash(sid),),
        ).fetchone()
        if (
            user is None
            or user["last_seen"] <= now - IDLE_SECONDS
            or user["created_at"] <= now - ABSOLUTE_SECONDS
        ):
            session.clear()
            try:
                with connection:
                    connection.execute(
                        "DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(sid),)
                    )
            except sqlite3.OperationalError:
                current_app.logger.warning("Expired session cleanup skipped: database unavailable")
        else:
            g.user = user
            if user["last_seen"] <= now - 60:
                try:
                    with connection:
                        connection.execute(
                            "UPDATE auth_sessions SET last_seen=? WHERE token_hash=?",
                            (now, token_hash(sid)),
                        )
                except sqlite3.OperationalError:
                    # A failed refresh can only shorten this session's life.
                    # Authentication and expiry checks above still fail closed.
                    current_app.logger.warning("Session refresh skipped: database unavailable")
    if request.method == "POST":
        expected = session.get("csrf", "")
        supplied = request.form.get("csrf", "")
        if (
            not expected
            or not supplied
            or len(supplied) > 128
            or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
        ):
            abort(400, description="页面已失效或表单无法验证，请刷新页面后重试。")


def login_required(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapped


def reserve_login_attempt():
    """Reserve before hashing, atomically across all Gunicorn workers.

    Only one trusted Caddy proxy supplies remote_addr. HMAC keeps raw IPs out
    of the database. Group IPv6 addresses by /64 to limit address rotation.
    """
    try:
        address = ipaddress.ip_address(request.remote_addr)
    except ValueError:
        abort(400)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    client = (
        str(ipaddress.ip_network(f"{address}/64", strict=False))
        if address.version == 6
        else str(address)
    )
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    client_hash = hmac.new(key, client.encode("ascii"), hashlib.sha256).hexdigest()
    now = int(time.time())
    connection = get_db()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM login_limits WHERE window_start <= ?", (now - LOGIN_WINDOW,)
        )
        row = connection.execute(
            "SELECT * FROM login_limits WHERE client_hash=?", (client_hash,)
        ).fetchone()
        if row and row["attempts"] >= LOGIN_ATTEMPTS:
            return max(1, row["window_start"] + LOGIN_WINDOW - now)
        if row:
            connection.execute(
                "UPDATE login_limits SET attempts=attempts+1 WHERE client_hash=?", (client_hash,)
            )
        else:
            connection.execute("INSERT INTO login_limits VALUES(?,?,1)", (client_hash, now))
    return 0


def start_session(user):
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    connection = get_db()
    with connection:
        connection.execute(
            "DELETE FROM auth_sessions WHERE last_seen <= ? OR created_at <= ?",
            (now - IDLE_SECONDS, now - ABSOLUTE_SECONDS),
        )
        connection.execute(
            "INSERT INTO auth_sessions VALUES(?,?,?,?,?)",
            (token_hash(sid), user["username"], user["generation"], now, now),
        )
    session.clear()
    session["sid"] = sid
    session.permanent = True


def end_session():
    sid = session.get("sid")
    # Drop the cookie first so a locked or full database cannot keep it valid.
    session.clear()
    if sid:
        try:
            connection = get_db()
            with connection:
                connection.execute(
                    "DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(sid),)
                )
        except sqlite3.OperationalError:
            current_app.logger.warning("Session revocation skipped: database unavailable")


def set_password(username, password):
    """CLI-only: update credentials and revoke every existing session."""
    if not username or username != username.strip() or len(username) > 100:
        raise ValueError("账号不能为空、不能以空格开头或结尾，且不能超过 100 字符。")
    if not 14 <= len(password) <= 1024:
        raise ValueError("密码长度须为 14 至 1024 字符。")
    password_hash = generate_password_hash(password)
    connection = get_db()
    with connection:
        connection.execute(
            """INSERT INTO users(username,password) VALUES(?,?)
            ON CONFLICT(username) DO UPDATE
            SET password=excluded.password, generation=generation+1""",
            (username, password_hash),
        )
        connection.execute("DELETE FROM auth_sessions WHERE username=?", (username,))
