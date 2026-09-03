"""
System status & health checks (*arr-style, System -> Status).

Pure-ish helpers (stdlib only, no FastAPI) that inspect the runtime:
- general info (versions, database, uptime)
- storage (free space on the mounted volumes)
- worker state (heartbeat written by the worker process)
- health checks: a list of PROBLEMS only (warning/error), like the *arr
  family — the UI shows "no issues" when the list is empty.

SQLAlchemy models are used only through the session passed in (no imports
of the app modules, avoiding import cycles).
"""

import os
import shutil
import sqlite3
import platform
import time
from datetime import datetime, timezone

import models
import backups
import settings

# Worker liveness thresholds (see worker.py heartbeat)
WORKER_STALE_SECONDS = 120
JOB_STALE_MINUTES = 60

# Storage thresholds (transcoding needs headroom)
TEMP_ERROR_GB = 2
TEMP_WARN_GB = 10
MEDIA_WARN_GB = 10
DATA_WARN_GB = 1

# Volumes mounted in the container (docker-compose)
STORAGE_PATHS = ("/data", "/media", "/temp")


# -------------------------------------------------
# Time helpers (naive UTC, SQLite-compatible)
# -------------------------------------------------

def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_setting_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp stored in the settings table."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# -------------------------------------------------
# Formatting
# -------------------------------------------------

def format_bytes(size: int) -> str:
    """Human-readable size (B / KB / MB / GB / TB)."""
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def format_duration(seconds: float | None) -> str:
    """Human-readable duration: '3d 4h 5m' / '42s'."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


# -------------------------------------------------
# Database info
# -------------------------------------------------

def db_info(db_path: str) -> dict:
    size = 0
    exists = os.path.exists(db_path)
    if exists:
        try:
            size = os.path.getsize(db_path)
        except OSError:
            size = 0
    return {"path": db_path, "exists": exists, "size": size}


# PRAGMA integrity_check reads the whole database: cache the result for a
# few minutes so System -> Status (and the hourly worker health notify) do
# not re-scan it on every visit.
_INTEGRITY_CACHE: dict[str, tuple[float, bool]] = {}
INTEGRITY_CACHE_TTL_SECONDS = 300.0


def _db_integrity_check(db_path: str) -> bool:
    """Quick PRAGMA integrity_check on the live database (read-only)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return bool(row and row[0] == "ok")
        finally:
            con.close()
    except Exception:
        return False


def db_integrity_ok(db_path: str) -> bool:
    """db_integrity_ok with a short TTL cache (see INTEGRITY_CACHE_TTL_SECONDS)."""
    now = time.monotonic()
    cached = _INTEGRITY_CACHE.get(db_path)
    if cached is not None and now - cached[0] < INTEGRITY_CACHE_TTL_SECONDS:
        return cached[1]
    result = _db_integrity_check(db_path)
    _INTEGRITY_CACHE[db_path] = (now, result)
    return result


# -------------------------------------------------
# Storage
# -------------------------------------------------

def storage_info(paths: tuple[str, ...] = STORAGE_PATHS) -> list[dict]:
    """Free/total/used per mounted path (missing paths are flagged)."""
    result = []
    for path in paths:
        entry = {"path": path, "exists": False, "total": 0, "used": 0, "free": 0}
        try:
            usage = shutil.disk_usage(path)
            entry.update(
                {
                    "exists": True,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                }
            )
        except OSError:
            pass
        result.append(entry)
    return result


# -------------------------------------------------
# Worker state (heartbeat written by worker.py)
# -------------------------------------------------

def _get_setting(db, key: str) -> str | None:
    return settings.get_setting(db, key)


def worker_state(db) -> dict:
    """
    Worker liveness from the heartbeat table.

    state: 'running' (heartbeat fresh) | 'processing' (heartbeat stale but a
    recent job is in progress) | 'stopped' (no fresh heartbeat, no active
    job) | 'never' (no heartbeat recorded at all).
    """
    hb = parse_setting_ts(_get_setting(db, "worker_heartbeat"))
    started = parse_setting_ts(_get_setting(db, "worker_started_at"))
    now = utcnow_naive()

    busy = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.status == "processing")
        .order_by(models.MediaFile.started_at.desc())
        .first()
    )

    age = (now - hb).total_seconds() if hb else None
    state = "never"
    alive = False
    busy_minutes = None

    if hb is not None:
        if age is not None and age <= WORKER_STALE_SECONDS:
            state, alive = "running", True
        elif busy is not None and busy.started_at is not None:
            busy_minutes = (now - busy.started_at).total_seconds() / 60
            if busy_minutes <= JOB_STALE_MINUTES:
                state, alive = "processing", True
            else:
                state = "stopped"
        else:
            state = "stopped"

    return {
        "alive": alive,
        "state": state,
        "last_seen": hb,
        "age_seconds": age,
        "started_at": started,
        "busy_file": busy.file_name if busy else None,
        "busy_minutes": busy_minutes,
    }


# -------------------------------------------------
# Health checks (problems only, *arr style)
# -------------------------------------------------

def run_health_checks(db, db_path: str) -> list[dict]:
    """
    Return a list of PROBLEMS: [{"level": "warning"|"error", "source": ...,
    "key": "health.<id>", "args": {...}}]. Empty list = everything is fine.
    """
    issues: list[dict] = []

    # --- Worker / tooling ---
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        issues.append(
            {
                "level": "error",
                "source": "Worker",
                "key": "health.ffmpeg_missing",
                "args": {},
            }
        )

    ws = worker_state(db)
    if ws["state"] == "never":
        issues.append(
            {"level": "error", "source": "Worker", "key": "health.worker_never", "args": {}}
        )
    elif ws["state"] == "stopped":
        issues.append(
            {
                "level": "error",
                "source": "Worker",
                "key": "health.worker_stopped",
                "args": {},
            }
        )

    stale = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.status == "processing",
            models.MediaFile.started_at.isnot(None),
        )
        .all()
    )
    stale_count = 0
    for job in stale:
        if job.started_at is not None:
            age_min = (utcnow_naive() - job.started_at).total_seconds() / 60
            if age_min > JOB_STALE_MINUTES:
                stale_count += 1
    if stale_count:
        issues.append(
            {
                "level": "warning",
                "source": "Worker",
                "key": "health.stale_jobs",
                "args": {"count": stale_count},
            }
        )

    # --- Hardware acceleration: requested backend fell back to CPU ---
    hw_req = _get_setting(db, "hwaccel_requested")
    if hw_req and hw_req != "cpu":
        hw_eff = _get_setting(db, "hwaccel_effective") or "cpu"
        hw_ok = _get_setting(db, "hwaccel_ok") or "0"
        hw_detail = _get_setting(db, "hwaccel_detail") or ""
        if hw_eff == "cpu" or hw_ok != "1":
            issues.append(
                {
                    "level": "warning",
                    "source": "Worker",
                    "key": "health.hwaccel_fallback",
                    "args": {
                        "requested": hw_req,
                        "effective": hw_eff,
                        "detail": hw_detail,
                    },
                }
            )

    # --- Media: files blocked by unknown language (und) ---
    blocked = 0
    for mf in (
        db.query(models.MediaFile)
        .filter(models.MediaFile.status.in_(("pending", "queued")))
        .all()
    ):
        langs = f"{mf.audio_languages or ''},{mf.subtitle_languages or ''}"
        if any(lang.strip() == "und" for lang in langs.split(",")):
            blocked += 1
    if blocked:
        issues.append(
            {
                "level": "warning",
                "source": "Media",
                "key": "health.und_blocked",
                "args": {"count": blocked},
            }
        )

    # --- Storage ---
    for entry in storage_info():
        free_gb = entry["free"] / (1024**3)
        if entry["path"] == "/temp":
            if not entry["exists"]:
                issues.append(
                    {"level": "error", "source": "Storage", "key": "health.temp_missing", "args": {}}
                )
            elif free_gb < TEMP_ERROR_GB:
                issues.append(
                    {
                        "level": "error",
                        "source": "Storage",
                        "key": "health.temp_low",
                        "args": {"path": "/temp", "free": format_bytes(entry["free"])},
                    }
                )
            elif free_gb < TEMP_WARN_GB:
                issues.append(
                    {
                        "level": "warning",
                        "source": "Storage",
                        "key": "health.temp_low",
                        "args": {"path": "/temp", "free": format_bytes(entry["free"])},
                    }
                )
        elif entry["path"] == "/media":
            if not entry["exists"]:
                issues.append(
                    {"level": "warning", "source": "Storage", "key": "health.media_missing", "args": {}}
                )
            elif free_gb < MEDIA_WARN_GB:
                issues.append(
                    {
                        "level": "warning",
                        "source": "Storage",
                        "key": "health.media_low",
                        "args": {"path": "/media", "free": format_bytes(entry["free"])},
                    }
                )
        elif entry["path"] == "/data":
            if not entry["exists"]:
                issues.append(
                    {"level": "warning", "source": "Storage", "key": "health.data_missing", "args": {}}
                )
            elif free_gb < DATA_WARN_GB:
                issues.append(
                    {
                        "level": "warning",
                        "source": "Storage",
                        "key": "health.data_low",
                        "args": {"path": "/data", "free": format_bytes(entry["free"])},
                    }
                )

    # --- Database ---
    if not db_integrity_ok(db_path):
        issues.append(
            {"level": "error", "source": "Database", "key": "health.db_integrity", "args": {}}
        )

    # --- Backups ---
    try:
        interval = int(_get_setting(db, "backup_interval_days") or "0")
    except ValueError:
        interval = 0
    if interval > 0 and not backups.list_backups(backups.BACKUP_DIR):
        issues.append(
            {"level": "warning", "source": "Backups", "key": "health.no_backups", "args": {}}
        )

    # --- Recycle bin ---
    if _get_setting(db, "recycle_bin_enabled") == "1" and not (
        _get_setting(db, "recycle_bin_path") or ""
    ).strip():
        issues.append(
            {"level": "warning", "source": "Media", "key": "health.recycle_path_empty", "args": {}}
        )

    # --- Authentication ---
    if _get_setting(db, "auth_enabled") != "1":
        issues.append(
            {"level": "warning", "source": "General", "key": "health.auth_disabled", "args": {}}
        )

    # --- Libraries: media/temp paths must exist ---
    for lib in db.query(models.Library).all():
        missing = []
        if lib.media_path and not os.path.exists(lib.media_path):
            missing.append(lib.media_path)
        if lib.temp_path and not os.path.exists(lib.temp_path):
            missing.append(lib.temp_path)
        if missing:
            issues.append(
                {
                    "level": "warning",
                    "source": "Media",
                    "key": "health.library_missing",
                    "args": {"name": lib.name, "path": ", ".join(missing)},
                }
            )

    return issues


# Re-export for convenience (used by main.py)
def python_version() -> str:
    return platform.python_version()


def sqlite_version() -> str:
    return sqlite3.sqlite_version
