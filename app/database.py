from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
import os
import shutil
from datetime import datetime

# Database location is configurable via THRESHERR_DB_PATH.
# Default: /data/thresherr.db (dedicated data volume, separated from code).
DB_PATH = os.environ.get("THRESHERR_DB_PATH", "/data/thresherr.db")

# Ensure the parent directory exists (e.g. local dev without the volume)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# -------------------------------------------------
# Staged restore (System -> Backups -> Restore)
# -------------------------------------------------
# A restore is staged as thresherr.db.pending_restore next to the live
# database. It is applied HERE, before the engine is created, so no process
# has the database open yet. Replacing a live WAL database behind running
# processes (app + worker) could corrupt data or silently lose writes, so
# restoring always requires a stack restart (docker compose restart).
def _apply_pending_restore() -> None:
    pending = os.path.join(
        os.path.dirname(DB_PATH), "thresherr.db.pending_restore"
    )
    if not os.path.exists(pending):
        return
    try:
        # Safety copy of the current database (complete picture incl. WAL/SHM)
        if os.path.exists(DB_PATH):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                src = DB_PATH + suffix
                if os.path.exists(src):
                    shutil.copy2(src, f"{DB_PATH}.pre-restore-{ts}{suffix}")
            print(f"[database] pre-restore safety copy: {DB_PATH}.pre-restore-{ts}")

        os.replace(pending, DB_PATH)
        # Stale WAL/SHM belong to the OLD database file; without removing them
        # SQLite could try to recover the old WAL against the new file.
        for suffix in ("-wal", "-shm"):
            stale = DB_PATH + suffix
            if os.path.exists(stale):
                os.remove(stale)
        print(f"[database] staged restore applied: {DB_PATH}")
    except Exception as exc:
        print(f"[database] FAILED to apply staged restore: {exc}")


_apply_pending_restore()

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)


# -------------------------------------------------
# SQLite WAL mode (concurrency safety)
# -------------------------------------------------
# The app (FastAPI, concurrent requests) and the worker (separate process)
# access the same SQLite database. In the default 'delete' journal mode,
# every write locks the whole database file and concurrent readers/writers
# can hit 'database is locked'. WAL allows readers to run concurrently with
# a single writer and is the recommended mode for this workload.
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_schema_columns() -> None:
    """Lightweight additive migrations for existing databases.

    create_all only creates missing tables, never missing columns on
    existing ones. Additive ALTERs live here and run at import time in
    every process (app + worker).
    """
    try:
        import sqlite3

        con = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cols = [
                r[1] for r in con.execute("PRAGMA table_info(media_files)")
            ]
            if cols and "video_bitrate" not in cols:
                con.execute(
                    "ALTER TABLE media_files ADD COLUMN video_bitrate INTEGER"
                )
                con.commit()
                print("[database] added column media_files.video_bitrate")
        finally:
            con.close()
    except Exception as exc:
        print(f"[database] schema ensure skipped/failed: {exc}")


_ensure_schema_columns()

Base = declarative_base()

def get_db():
    """
    Dependency to provide a database session to FastAPI endpoints.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
