"""
Scheduled tasks (*arr-style, System -> Tasks).

The worker runs scheduled tasks in its ~hourly housekeeping block (scan +
recycle bin cleanup + automatic backups); the UI can also execute them on
demand (Execute button). Every execution records its outcome in the
settings table (last run / duration / result) so the Tasks page shows the
same information the *arr family shows: interval, last execution, duration,
result.

Imports are kept cycle-free: tasks.py imports scanner/models/backups, and
worker.py imports tasks.
"""

import os
import time
from datetime import datetime, timezone

import models
from scanner import scan_libraries, clear_stale_scanning
import backups
import settings
import language_detect
import connect
from logging_setup import get_logger

logger = get_logger("tasks")

# Task identifiers, in UI order
TASK_SCAN = "scan"
TASK_RECYCLE = "recycle"
TASK_BACKUP = "backup"
ALL_TASKS = (TASK_SCAN, TASK_RECYCLE, TASK_BACKUP)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Settings helpers come from the shared settings module
_get_setting = settings.get_setting
_set_setting = settings.set_setting


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# -------------------------------------------------
# SCAN LIBRARIES
# -------------------------------------------------

def scan_interval_hours(db) -> int:
    """Hours between automatic scans; 0 = disabled."""
    try:
        return max(0, int(_get_setting(db, "scan_interval_hours", "0") or "0"))
    except ValueError:
        return 0


def _set_status_bulk(db, ids, status: str, only_if: str | None = None) -> int:
    """Bulk status change with NO ORM rowcount check.

    Used for the transient 'scanning' badge. The old dirty-object flush raised
    StaleDataError ("expected to update 85 row(s); 61 were matched") when
    another process removed some of those rows meanwhile (deleting a library
    cascades to its media_files), and that killed the whole language pass.
    A bulk UPDATE cannot fail that way. Chunked to stay under SQLite's
    parameter limit.
    """
    total = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        query = db.query(models.MediaFile).filter(models.MediaFile.id.in_(chunk))
        if only_if is not None:
            query = query.filter(models.MediaFile.status == only_if)
        total += query.update(
            {models.MediaFile.status: status}, synchronize_session=False
        )
        db.commit()
    return total


def _resolve_language(media, db, resolver) -> bool:
    """Run one language resolver for one file.

    A row can vanish mid-pass (deleting a library cascades to its files); that
    must skip the file, never abort the whole cascade.
    """
    try:
        return bool(resolver(media, db))
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Language pass skipped media_file id=%s (%s): %s",
            media.id, media.file_name, exc,
        )
        return False


def run_language_cascade(db) -> dict:
    """
    Resolve 'und' files automatically after a scan, in decreasing scope:

      Pass 1: mkvinfo/mkvmerge over the 'und' files (Matroska) — tags the
              container with mkvpropedit when a language is found.
      Pass 2: mediainfo (audio) + fastText (text subs) over the remaining.

    Files being analysed get the transient 'scanning' status (WORKING badge)
    and are restored to 'pending' when the cascade finishes.
    """
    stats = {"mkvinfo": 0, "mediainfo": 0, "fasttext": 0, "und_remaining": 0}

    und_files = [
        mf
        for mf in db.query(models.MediaFile)
        .filter(models.MediaFile.status == "pending")
        .all()
        if language_detect.has_und_in_summary(mf)
    ]
    if not und_files:
        return stats

    # Capture id/path up front: a commit expires every loaded object, so
    # touching attributes later could trigger a reload of a deleted row.
    targets = [(mf.id, (mf.full_path or "")) for mf in und_files]
    _set_status_bulk(db, [t[0] for t in targets], "scanning")

    try:
        # Pass 1: mkvinfo (Matroska only)
        remaining = []
        for mf, (mf_id, mf_path) in zip(und_files, targets):
            if not mf_path.lower().endswith(".mkv"):
                remaining.append((mf, mf_id, mf_path))
                continue
            if _resolve_language(mf, db, language_detect.resolve_with_mkvinfo):
                stats["mkvinfo"] += 1
            else:
                remaining.append((mf, mf_id, mf_path))

        # Pass 2: mediainfo (audio) + fastText (text subs) over the rest
        for mf, _mf_id, _mf_path in remaining:
            if _resolve_language(mf, db, language_detect.resolve_with_mediainfo):
                stats["mediainfo"] += 1
    finally:
        # Only rows still in the transient state go back to the queue.
        _set_status_bulk(db, [t[0] for t in targets], "pending", only_if="scanning")

    und_remaining = 0
    for mf in und_files:
        try:
            if language_detect.has_und_in_summary(mf):
                und_remaining += 1
        except Exception:
            continue
    stats["und_remaining"] = und_remaining
    return stats


def run_scan(db, progress=None) -> dict:
    """
    Run a library scan right now and record the outcome.
    Returns {"ok": bool, "new_count"|"error": ..., "duration": seconds}.

    Safe to call from any process or thread: the 'scan_running' setting
    acts as a cross-process lock, so a manual scan (app background
    thread) and a scheduled scan (worker housekeeping) can never overlap.
    The lock is always cleared in a finally block.

    progress: optional callable(done, total) invoked periodically while
    files are being probed (used to persist live progress for the UI).
    """
    start = time.monotonic()

    # Anti-overlap: one scan at a time, across processes.
    if _get_setting(db, "scan_running", "0") == "1":
        duration = round(time.monotonic() - start, 1)
        result = "scan already running"
        return {"ok": False, "error": result, "duration": duration, "result": result}

    _set_setting(db, "scan_running", "1")
    _set_setting(db, "scan_progress_done", "0")
    _set_setting(db, "scan_progress_total", "0")
    db.commit()
    try:
        # 'scanning' is a transient badge used while the language cascade runs.
        # The scan lock guarantees a single scan at a time, so anything still
        # carrying it belongs to a scan that died (crash, restart) and would
        # otherwise stay listed as "working" for ever, out of the queue.
        healed = clear_stale_scanning(db)
        if healed:
            logger.warning(
                "Cleared %s stale 'scanning' row(s) left by a dead scan", healed
            )
        new_count, refreshed_count, backfilled_count, removed_count = scan_libraries(db, progress=progress)
        cascade = run_language_cascade(db)
        duration = round(time.monotonic() - start, 1)
        resolved = cascade["mkvinfo"] + cascade["mediainfo"]
        result = f"{new_count} new file(s)"
        if refreshed_count:
            result += f" | {refreshed_count} replaced file(s) refreshed"
        if backfilled_count:
            result += f" | {backfilled_count} mtime recorded"
        if removed_count:
            result += f" | {removed_count} entry(s) removed (file gone)"
        if resolved:
            result += f" | {resolved} language(s) resolved"
        if cascade["und_remaining"]:
            result += f" | {cascade['und_remaining']} und left"
        _set_setting(db, "scan_last_run", _utcnow_naive().isoformat())
        _set_setting(db, "scan_last_duration", str(duration))
        _set_setting(db, "scan_last_result", result)
        db.commit()
        out = {
            "ok": True,
            "new_count": new_count,
            "refreshed_count": refreshed_count,
            "duration": duration,
            "result": result,
        }
        # Connect: only actionable scans notify (new files found).
        if new_count > 0:
            connect.fire_event(
                db,
                "ScanCompleted",
                {
                    "newFiles": new_count,
                    "result": result,
                    "duration": duration,
                },
            )
        return out
    except Exception as exc:
        db.rollback()
        duration = round(time.monotonic() - start, 1)
        result = f"error: {exc}"
        _set_setting(db, "scan_last_run", _utcnow_naive().isoformat())
        _set_setting(db, "scan_last_duration", str(duration))
        _set_setting(db, "scan_last_result", result)
        db.commit()
        return {"ok": False, "error": str(exc), "duration": duration, "result": result}
    finally:
        _set_setting(db, "scan_running", "0")
        db.commit()


def maybe_scheduled_scan(db) -> str | None:
    """
    Run the scan when its interval has elapsed (worker housekeeping).
    Returns the result string when a scan ran, None otherwise.
    """
    interval = scan_interval_hours(db)
    if interval <= 0:
        return None
    last = parse_ts(_get_setting(db, "scan_last_run"))
    if last is not None:
        if (_utcnow_naive() - last).total_seconds() < interval * 3600:
            return None
    out = run_scan(db)
    # The app is already scanning (manual run): not a worker failure and
    # nothing for the worker to report as a scheduled run.
    if not out.get("ok") and out.get("error") == "scan already running":
        return None
    return out.get("result")


def scan_state(db) -> dict:
    return {
        "interval_hours": scan_interval_hours(db),
        "last_run": parse_ts(_get_setting(db, "scan_last_run")),
        "last_duration": _get_setting(db, "scan_last_duration"),
        "last_result": _get_setting(db, "scan_last_result"),
        # Live state for the UI (background scan):
        "running": _get_setting(db, "scan_running", "0") == "1",
        "progress_done": int(_get_setting(db, "scan_progress_done", "0") or "0"),
        "progress_total": int(_get_setting(db, "scan_progress_total", "0") or "0"),
    }


# -------------------------------------------------
# RECYCLE BIN CLEANUP (moved here from worker.py)
# -------------------------------------------------

def recycle_config(db) -> tuple[bool, str, int]:
    """(enabled, path, cleanup_days) from the settings table."""
    enabled = _get_setting(db, "recycle_bin_enabled", "0") == "1"
    path = (_get_setting(db, "recycle_bin_path") or "").strip()
    try:
        days = int(_get_setting(db, "recycle_bin_days", "7") or "7")
    except ValueError:
        days = 7
    return enabled, path, days


def cleanup_recycle_bin(recycle_dir: str, days: int) -> int:
    """Delete files older than `days` days; returns the number removed."""
    if not recycle_dir or not os.path.isdir(recycle_dir):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for entry in os.listdir(recycle_dir):
        full = os.path.join(recycle_dir, entry)
        if not os.path.isfile(full):
            continue
        try:
            if os.path.getmtime(full) < cutoff:
                os.remove(full)
                removed += 1
        except OSError:
            pass
    return removed


def run_recycle_cleanup(db) -> dict:
    """Run the recycle bin cleanup right now and record the outcome."""
    start = time.monotonic()
    try:
        enabled, path, days = recycle_config(db)
        removed = cleanup_recycle_bin(path, days) if (enabled and path) else 0
        duration = round(time.monotonic() - start, 1)
        result = f"removed {removed} file(s)" if removed else "nothing to clean"
        _set_setting(db, "recycle_last_run", _utcnow_naive().isoformat())
        _set_setting(db, "recycle_last_duration", str(duration))
        _set_setting(db, "recycle_last_result", result)
        db.commit()
        return {"ok": True, "removed": removed, "duration": duration, "result": result}
    except Exception as exc:
        db.rollback()
        duration = round(time.monotonic() - start, 1)
        result = f"error: {exc}"
        _set_setting(db, "recycle_last_run", _utcnow_naive().isoformat())
        _set_setting(db, "recycle_last_duration", str(duration))
        _set_setting(db, "recycle_last_result", result)
        db.commit()
        return {"ok": False, "error": str(exc), "duration": duration, "result": result}


def recycle_state(db) -> dict:
    return {
        "last_run": parse_ts(_get_setting(db, "recycle_last_run")),
        "last_duration": _get_setting(db, "recycle_last_duration"),
        "last_result": _get_setting(db, "recycle_last_result"),
    }


# -------------------------------------------------
# BACKUP (state derived from backups.py; execution lives in main.py)
# -------------------------------------------------

def backup_state(db) -> dict:
    try:
        interval = max(0, int(_get_setting(db, "backup_interval_days", "0") or "0"))
    except ValueError:
        interval = 0
    latest = backups.list_backups(backups.BACKUP_DIR)[:1]
    last_run = None
    last_name = None
    if latest:
        last_run = datetime.fromtimestamp(latest[0]["mtime"], tz=timezone.utc).replace(tzinfo=None)
        last_name = latest[0]["name"]
    return {
        "interval_days": interval,
        "last_run": last_run,
        "last_name": last_name,
    }
