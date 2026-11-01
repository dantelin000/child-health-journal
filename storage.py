"""SQLite access and additive, transactional schema migrations."""

import sqlite3
from pathlib import Path

from flask import current_app, g


def get_db(*, create=False):
    if "db" not in g:
        database = Path(current_app.config["DATABASE"]).resolve().as_uri()
        mode = "rwc" if create else "rw"
        g.db = sqlite3.connect(
            f"{database}?mode={mode}", uri=True, timeout=current_app.config["DATABASE_TIMEOUT"]
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    """Keep existing users and health records; never rebuild or drop tables."""
    connection = get_db(create=True)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, password TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY, child TEXT NOT NULL, occurred TEXT NOT NULL,
            temperature TEXT, symptoms TEXT, medication TEXT, care TEXT, notes TEXT,
            created TEXT NOT NULL, updated TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1)""")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
        if "version" not in columns:
            connection.execute("ALTER TABLE records ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        connection.execute("""CREATE INDEX IF NOT EXISTS records_occurred_id
            ON records(occurred DESC, id DESC)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
            generation INTEGER NOT NULL, created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL)""")
        connection.execute("""CREATE INDEX IF NOT EXISTS sessions_username
            ON auth_sessions(username)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS login_limits (
            client_hash TEXT PRIMARY KEY, window_start INTEGER NOT NULL,
            attempts INTEGER NOT NULL)""")
    Path(current_app.config["DATABASE"]).chmod(0o600)
