"""
Backups (*arr-style, System -> Backups).

Consistent SQLite backups of the application database using the Online
Backup API (sqlite3.Connection.backup), stored as timestamped ZIP files:

    thresherr_backup_YYYYMMDD_HHMMSS.zip   (contains thresherr.db)

Restore is STAGED: the new database is extracted, validated (SQLite header +
PRAGMA integrity_check) and moved to <db_dir>/thresherr.db.pending_restore.
database.py applies it on the next startup, BEFORE the engine is created
(i.e. before any process opens the database). A stack restart is therefore
required to complete a restore — this avoids replacing a live WAL database
behind running processes (app + worker), which could corrupt data or lose
silently the writes of stale connections.

This module is pure stdlib on purpose (no FastAPI/SQLAlchemy imports).
"""

import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime

# Default location for backups (inside the data volume, visible from the host).
BACKUP_DIR = os.environ.get("THRESHERR_BACKUP_DIR", "/data/backups")

# Name of the staged restore file, next to the live database.
PENDING_RESTORE_NAME = "thresherr.db.pending_restore"

# Strict naming: thresherr_backup_20260826_044500.zip
# (a collision suffix _1/_2/... is appended when two backups share a second)
_BACKUP_NAME_RE = re.compile(r"^thresherr_backup_\d{8}_\d{6}(?:_\d+)?\.zip$")


# -------------------------------------------------
# Validation helpers
# -------------------------------------------------

def is_valid_backup_name(name: str) -> bool:
    """Strict name check: no paths, no traversal, exact timestamp format."""
    return bool(_BACKUP_NAME_RE.fullmatch(name or ""))


def safe_backup_path(backup_dir: str, name: str) -> str | None:
    """Absolute path for a valid existing backup, or None."""
    if not is_valid_backup_name(name):
        return None
    path = os.path.join(backup_dir, name)
    if not os.path.isfile(path):
        return None
    return path


# -------------------------------------------------
# Listing & formatting
# -------------------------------------------------

def list_backups(backup_dir: str) -> list[dict]:
    """Backups sorted newest-first: [{name, path, size, mtime}]."""
    entries = []
    if os.path.isdir(backup_dir):
        for entry in os.listdir(backup_dir):
            if not is_valid_backup_name(entry):
                continue
            full = os.path.join(backup_dir, entry)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append(
                {
                    "name": entry,
                    "path": full,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def format_size(size: int) -> str:
    """Human-readable size for the backups table."""
    if size < 1024:
        return f"{size} B"
    if size < 1048576:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1048576:.1f} MB"


# -------------------------------------------------
# Backup creation
# -------------------------------------------------

def create_backup(db_path: str, backup_dir: str) -> str:
    """
    Consistent snapshot of the live SQLite database -> timestamped ZIP.

    Uses the Online Backup API (sqlite3.Connection.backup), which produces
    a coherent copy even while the app and the worker are writing (WAL-safe,
    no exclusive locks). The snapshot is integrity-checked before packing.

    Returns the created backup file path. Raises RuntimeError on failure.
    """
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Unique name: two backups in the same second must not overwrite each other
    base = f"thresherr_backup_{ts}"
    zip_path = os.path.join(backup_dir, f"{base}.zip")
    counter = 1
    while os.path.exists(zip_path):
        zip_path = os.path.join(backup_dir, f"{base}_{counter}.zip")
        counter += 1
    tmp_zip = zip_path + ".tmp"
    tmp_db = os.path.join(backup_dir, f".thresherr_backup_{ts}.db.tmp")

    try:
        # 1. Consistent copy of the live database (WAL-safe)
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        # 2. Sanity-check the snapshot before packing it
        with sqlite3.connect(tmp_db) as check:
            row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(f"integrity check failed: {row[0]!r}")

        # 3. Pack as ZIP (thresherr.db inside, like the *arr family)
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "thresherr.db")

        os.replace(tmp_zip, zip_path)
    finally:
        # Clean the temp snapshot AND its SQLite satellites (-wal/-shm)
        for p in (tmp_db, tmp_zip, tmp_db + "-wal", tmp_db + "-shm"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    return zip_path


def enforce_retention(backup_dir: str, retention: int) -> int:
    """Delete oldest backups beyond `retention`; returns the number removed."""
    retention = max(1, int(retention))
    removed = 0
    for old in list_backups(backup_dir)[retention:]:
        try:
            os.remove(old["path"])
            removed += 1
        except OSError:
            pass
    return removed


# -------------------------------------------------
# Staged restore
# -------------------------------------------------

def pending_restore_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(db_path), PENDING_RESTORE_NAME)


def pending_restore_exists(db_path: str) -> bool:
    return os.path.exists(pending_restore_path(db_path))


def stage_restore(backup_dir: str, name: str, db_path: str) -> str:
    """
    Validate a backup ZIP and stage it for the next startup.

    - Validates the file name and ZIP structure (no traversal members).
    - Extracts thresherr.db and runs SQLite header + integrity checks.
    - Moves it to <db_dir>/thresherr.db.pending_restore.

    database.py applies it on next startup (before the engine opens).

    Returns the pending path. Raises ValueError on validation failure.
    """
    zip_path = safe_backup_path(backup_dir, name)
    if not zip_path:
        raise ValueError(f"invalid or missing backup: {name}")

    tmp_dir = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if "thresherr.db" not in names:
                raise ValueError("backup does not contain thresherr.db")
            # Defensive: reject absolute or traversal member paths
            for member in names:
                if member.startswith("/") or ".." in member.split("/"):
                    raise ValueError(f"unsafe member in backup: {member}")
            tmp_dir = zip_path + ".restore_tmp"
            os.makedirs(tmp_dir, exist_ok=True)
            zf.extract("thresherr.db", tmp_dir)
        tmp_db = os.path.join(tmp_dir, "thresherr.db")

        # Must look like a real SQLite database
        with open(tmp_db, "rb") as fh:
            if fh.read(16) != b"SQLite format 3\x00":
                raise ValueError("extracted file is not a SQLite database")
        with sqlite3.connect(tmp_db) as check:
            row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ValueError(f"integrity check failed: {row[0]!r}")

        pending = pending_restore_path(db_path)
        shutil.move(tmp_db, pending)
        return pending
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# -------------------------------------------------
# Deletion
# -------------------------------------------------

def delete_backup(backup_dir: str, name: str) -> bool:
    """Delete a backup file; returns False when missing/invalid."""
    zip_path = safe_backup_path(backup_dir, name)
    if not zip_path:
        return False
    try:
        os.remove(zip_path)
        return True
    except OSError:
        return False
