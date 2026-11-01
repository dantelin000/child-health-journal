"""Create a verified SQLite snapshot and publish it without overwriting backups."""

import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def create_backup(source, directory):
    source = Path(source).resolve(strict=True)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".health-", suffix=".partial", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary)
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise TimeoutError("Database backup exceeded 60 seconds.")

    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as src,
            closing(sqlite3.connect(temporary, timeout=10)) as dst,
        ):
            src.backup(dst, pages=128, progress=progress)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Backup integrity check failed.")
            if dst.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Backup foreign-key check failed.")
        with temporary.open("rb") as snapshot:
            os.fsync(snapshot.fileno())
        stamp = datetime.now(ZoneInfo("Australia/Melbourne")).strftime("%Y%m%d-%H%M%S-%f")
        # The random tempfile component prevents collisions; link refuses overwrites.
        target = directory / f"health-{stamp}-{temporary.stem[8:]}.sqlite3"
        os.link(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return target
    finally:
        # Only this invocation's incomplete temporary file is removed.
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    result = create_backup("/var/lib/child-health/health.sqlite3", "/data/child-health-backups")
    print("Backup verified:", result)
