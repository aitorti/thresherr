from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import APIRouter
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import engine, SessionLocal, DB_PATH
from scanner import scan_libraries, get_video_metadata
from typing import Optional, Dict
from logging_setup import (
    TRACE,
    LOG_DIR,
    setup_logging,
    get_logger,
    get_log_level,
    set_log_level,
)
import i18n
import naming
import backups
import health
import tasks

import models
import subprocess
import json
import os
import glob
import re
import time
import hmac
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

APP_VERSION = "0.1.0"

# Process start time (naive UTC) for the System -> Status uptime display
APP_START_TIME = datetime.now(timezone.utc).replace(tzinfo=None)

# 1. Database setup
models.Base.metadata.create_all(bind=engine)

# 2. App & Templates initialization
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 2b. Logging (*arr-style: rolling files + SQLite table, System -> Logs)
setup_logging()
ui_logger = get_logger("ui")


# 2c. Request logging (visible in thresherr.trace.txt, like the *arr trace)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    get_logger("http").log(
        TRACE,
        "%s %s -> %s (%d ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response

# 3. Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------------------------------
# UI language + template rendering helper
# -------------------------------------------------

DATE_FORMATS = {
    "iso": "%Y-%m-%d",
    "eu": "%d/%m/%Y",
    "us": "%m/%d/%Y",
}
TIME_FORMATS = {
    "24": "%H:%M",
    "12": "%I:%M %p",
}


def _ui_language(db: Session) -> str:
    """UI language from settings; English when unset/invalid."""
    row = (
        db.query(models.Setting)
        .filter(models.Setting.key == "ui_language")
        .first()
    )
    if row and i18n.is_valid_language(row.value):
        return row.value
    return i18n.DEFAULT_LANGUAGE


def _make_fmt_dt(db: Session):
    """Return a fmt_dt(datetime) callable honouring Interface settings."""
    date_fmt = DATE_FORMATS.get(
        _get_setting(db, "date_format", "iso"), DATE_FORMATS["iso"]
    )
    time_fmt = TIME_FORMATS.get(
        _get_setting(db, "time_format", "24"), TIME_FORMATS["24"]
    )
    pattern = f"{date_fmt} {time_fmt}"

    def fmt_dt(dt):
        """Naive-UTC datetime -> Europe/Madrid with the configured pattern."""
        try:
            from zoneinfo import ZoneInfo
            from datetime import timezone as dt_timezone

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_timezone.utc)
            return dt.astimezone(ZoneInfo("Europe/Madrid")).strftime(pattern)
        except Exception:
            return dt.strftime(pattern)

    return fmt_dt


def render(request: Request, name: str, db: Session, **context) -> HTMLResponse:
    """
    Render a template with the *arr-style i18n helpers injected:
    - t(key, **kwargs): translate for the current UI language
    - lang: current language code
    - languages: available languages (code -> native name)
    - fmt_dt(datetime): date/time rendering per Interface settings
    """
    lang = _ui_language(db)
    context["t"] = i18n.translator(lang)
    context["lang"] = lang
    context["languages"] = i18n.LANGUAGES
    context["fmt_dt"] = _make_fmt_dt(db)
    context["current_user"] = _current_user(db, request)
    context["auth_enabled"] = _get_setting(db, "auth_enabled", "0") == "1"
    return templates.TemplateResponse(request=request, name=name, context=context)


# -------------------------------------------------
# UI Authentication (*arr style: Authentication: Forms)
# -------------------------------------------------

SESSION_COOKIE = "thresherr_session"
SESSION_DAYS = 30
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """PBKDF2-SHA256 with a random salt: 'salt$hex'."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    )
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
        )
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


def create_session(db: Session) -> str:
    """Create a session row and return its token."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(
        models.UserSession(
            token=token,
            created_at=now,
            expires_at=now + timedelta(days=SESSION_DAYS),
        )
    )
    db.commit()
    return token


def _current_user(db: Session, request: Request) -> str | None:
    """Username for the request session, or None (auth disabled or invalid)."""
    if _get_setting(db, "auth_enabled", "0") != "1":
        return None
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = (
        db.query(models.UserSession)
        .filter(models.UserSession.token == token)
        .first()
    )
    if not row:
        return None
    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        db.delete(row)
        db.commit()
        return None
    return _get_setting(db, "auth_username", "") or None


def delete_session(db: Session, token: str) -> None:
    row = (
        db.query(models.UserSession)
        .filter(models.UserSession.token == token)
        .first()
    )
    if row:
        db.delete(row)
        db.commit()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Enforce UI authentication when enabled (Settings -> General -> Security).

    Public: /login and /api/v1/* (API-key authenticated). Everything else
    requires a valid session cookie; HTML requests are redirected to the
    login page, API requests get 401.
    """
    path = request.url.path
    if path == "/login" or path.startswith("/api/v1"):
        return await call_next(request)

    db = SessionLocal()
    try:
        if _get_setting(db, "auth_enabled", "0") == "1":
            token = request.cookies.get(SESSION_COOKIE)
            if _current_user(db, request) is None:
                if path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": "Not authenticated"}, status_code=401
                    )
                return RedirectResponse(
                    url="/login?next=" + quote(path), status_code=303
                )
    finally:
        db.close()

    return await call_next(request)


# -------------------------------------------------
# Settings helpers (key/value table)
# -------------------------------------------------

def _get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if row and row.value is not None:
        return row.value
    return default


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))


def _recycle_stats(path: str) -> dict:
    """(count, size_mb) of files currently parked in the recycle bin."""
    count = 0
    total = 0
    if path and os.path.isdir(path):
        try:
            for entry in os.listdir(path):
                full = os.path.join(path, entry)
                if os.path.isfile(full):
                    count += 1
                    total += os.path.getsize(full)
        except OSError:
            pass
    return {"count": count, "size_mb": round(total / 1048576, 1)}


# -------------------------------------------------
# API Key (*arr-style, Settings -> General -> Security)
# -------------------------------------------------

def get_api_key(db: Session) -> str:
    """Current API key; auto-generated and persisted on first use."""
    row = (
        db.query(models.Setting)
        .filter(models.Setting.key == "api_key")
        .first()
    )
    if row and row.value:
        return row.value
    key = secrets.token_hex(16)
    db.add(models.Setting(key="api_key", value=key))
    db.commit()
    ui_logger.info("API key generated (first run)")
    return key


def reset_api_key(db: Session) -> str:
    """Generate and persist a fresh API key."""
    key = secrets.token_hex(16)
    row = (
        db.query(models.Setting)
        .filter(models.Setting.key == "api_key")
        .first()
    )
    if row:
        row.value = key
    else:
        db.add(models.Setting(key="api_key", value=key))
    db.commit()
    ui_logger.info("API key reset")
    return key


def require_api_key(request: Request, db: Session = Depends(get_db)) -> None:
    """
    Dependency for external API routes: header X-Api-Key or ?apikey=...
    Comparison is constant-time to avoid timing attacks.
    """
    provided = request.headers.get("X-Api-Key") or request.query_params.get("apikey")
    expected = get_api_key(db)
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# External API (v1) — the seed for *arr-style integrations
api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])


@api_v1.get("/system/status")
async def api_system_status(db: Session = Depends(get_db)):
    """Full status: versions, uptime, DB, worker, storage and health."""
    uptime_seconds = (
        datetime.now(timezone.utc).replace(tzinfo=None) - APP_START_TIME
    ).total_seconds()
    return {
        "app": "thresherr",
        "version": APP_VERSION,
        "python": health.python_version(),
        "sqlite": health.sqlite_version(),
        "uptime_seconds": round(uptime_seconds),
        "db": health.db_info(DB_PATH),
        "worker": health.worker_state(db),
        "storage": health.storage_info(),
        "health": health.run_health_checks(db, DB_PATH),
        "stats": compute_global_stats(db),
    }


@api_v1.get("/health")
async def api_health(db: Session = Depends(get_db)):
    """Health checks (problems only, *arr style). Empty = all good."""
    return {"health": health.run_health_checks(db, DB_PATH)}


@api_v1.get("/system/tasks")
async def api_list_tasks(db: Session = Depends(get_db)):
    """Scheduled tasks state (interval, last run, result)."""
    return {
        "tasks": {
            tasks.TASK_SCAN: tasks.scan_state(db),
            tasks.TASK_RECYCLE: tasks.recycle_state(db),
            tasks.TASK_BACKUP: tasks.backup_state(db),
        }
    }


@api_v1.post("/system/tasks/{task}/execute")
async def api_execute_task(task: str, db: Session = Depends(get_db)):
    """Execute a task right now (scan | recycle | backup)."""
    if task == tasks.TASK_SCAN:
        return tasks.run_scan(db)
    if task == tasks.TASK_RECYCLE:
        return tasks.run_recycle_cleanup(db)
    if task == tasks.TASK_BACKUP:
        try:
            out = _run_backup_now(db)
            out["result"] = f"backup created: {os.path.basename(out['path'])}"
            return out
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")
    raise HTTPException(status_code=404, detail="Unknown task")


@api_v1.get("/system/backups")
async def api_list_backups():
    """List available backups (newest first)."""
    return {"backups": backups.list_backups(backups.BACKUP_DIR)}


@api_v1.post("/system/backups")
async def api_create_backup(db: Session = Depends(get_db)):
    """Create a backup right now (manual/on-demand, e.g. from a cron)."""
    try:
        path = backups.create_backup(DB_PATH, backups.BACKUP_DIR)
        try:
            retention = int(_get_setting(db, "backup_retention", "7") or "7")
        except ValueError:
            retention = 7
        removed = backups.enforce_retention(backups.BACKUP_DIR, retention)
        ui_logger.info(
            "API backup created: %s (retention removed %s)",
            os.path.basename(path),
            removed,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Backup failed: {exc}")
    return {"id": os.path.basename(path), "path": path}


@api_v1.delete("/system/backups/{name}")
async def api_delete_backup(name: str):
    """Delete a backup by file name."""
    if not backups.delete_backup(backups.BACKUP_DIR, name):
        raise HTTPException(status_code=404, detail="Backup not found")
    ui_logger.info("API backup deleted: %s", name)
    return {"deleted": name}


app.include_router(api_v1)

# --- LOADING GLOBAL STATISTICS ---

SORT_FIELDS = ("title", "year", "size", "status", "library", "resolution")
VIEWS = ("table", "posters", "overview")

def compute_global_stats(db: Session) -> dict:
    """
    Compute global storage statistics used by the UI.
    Centralized to keep all templates dynamic (e.g., sidebar cards).
    """
    total_orig = db.query(func.sum(models.MediaFile.size_original)).scalar() or 0
    total_done_orig = (
        db.query(func.sum(models.MediaFile.size_original))
        .filter(models.MediaFile.status == "completed")
        .scalar()
        or 0
    )
    total_done_final = (
        db.query(func.sum(models.MediaFile.size_final))
        .filter(models.MediaFile.status == "completed")
        .scalar()
        or 0
    )

    savings = total_done_orig - total_done_final
    savings_pct = (savings / total_done_orig * 100) if total_done_orig > 0 else 0

    return {
        "total_gb": round(total_orig / (1024**3), 2),
        "processed_orig_gb": round(total_done_orig / (1024**3), 2),
        "processed_final_gb": round(total_done_final / (1024**3), 2),
        "savings_gb": round(savings / (1024**3), 2),
        "savings_pct": round(savings_pct, 1),
    }


# 4. Routes

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    q: str = "",
    view: str = "table",
    sort: str = "title",
    dir: str = "asc",
    status: str = "all",
    library: str = "",
    quality: str = "",
    audio: str = "",
    subs: str = "",
    db: Session = Depends(get_db),
):
    """Monitored media files — Radarr-style: view toggle, sorting, filters."""
    stats = compute_global_stats(db)

    # Sanitize view/sort/dir
    if view not in VIEWS:
        view = "table"
    if sort not in SORT_FIELDS:
        sort = "title"
    if dir not in ("asc", "desc"):
        dir = "asc"

    query = db.query(models.MediaFile)
    if q:
        query = query.filter(models.MediaFile.file_name.ilike(f"%{q}%"))
    if status != "all":
        query = query.filter(models.MediaFile.status == status)
    if library:
        query = query.filter(models.MediaFile.library_id == int(library))
    if quality:
        query = query.filter(models.MediaFile.resolution == quality)
    if audio:
        query = query.filter(models.MediaFile.audio_languages.ilike(f"%{audio}%"))
    if subs:
        query = query.filter(models.MediaFile.subtitle_languages.ilike(f"%{subs}%"))

    media_files = query.order_by(models.MediaFile.id.desc()).all()
    for mf in media_files:
        mf.has_stream_overrides = mf.stream_overrides is not None
        mf.year = naming.extract_year(mf.file_name)

    # Sorting (year is computed in Python, so sort here)
    def sort_key(f):
        if sort == "title":
            return f.file_name.lower()
        if sort == "year":
            return f.year or ""
        if sort == "size":
            return f.size_original or 0
        if sort == "status":
            return f.status
        if sort == "library":
            return f.library.name.lower() if f.library else ""
        if sort == "resolution":
            return f.resolution or ""
        return f.file_name.lower()

    media_files.sort(key=sort_key, reverse=(dir == "desc"))

    # Filter options (derived from real data)
    libraries = db.query(models.Library).order_by(models.Library.name).all()
    resolutions = sorted(
        {
            r[0]
            for r in db.query(models.MediaFile.resolution).distinct().all()
            if r[0]
        }
    )
    audio_langs = sorted(
        {
            lang.strip()
            for row in db.query(models.MediaFile.audio_languages).all()
            if row[0]
            for lang in row[0].split(",")
        }
    )
    sub_langs = sorted(
        {
            lang.strip()
            for row in db.query(models.MediaFile.subtitle_languages).all()
            if row[0]
            for lang in row[0].split(",")
        }
    )

    return render(
        request=request,
        name="dashboard.html",
        db=db,
        **stats,
        media_files=media_files,
        q=q,
        view=view,
        sort=sort,
        dir=dir,
        status=status,
        library=library,
        quality=quality,
        audio=audio,
        subs=subs,
        libraries=libraries,
        resolutions=resolutions,
        audio_langs=audio_langs,
        sub_langs=sub_langs,
    )

# --- WORKGIN WITH PROFILES ---

@app.get("/profiles", response_class=HTMLResponse)
async def get_profiles(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    profiles = db.query(models.Profile).all()
    return render(
        request=request,
        name="profiles.html",
        db=db,
        **stats,
        profiles=profiles,
    )

@app.post("/profiles")
async def create_profile(
    name: str = Form(...),
    video_codec: str = Form(...),
    container: str = Form(...),
    video_max_res: int = Form(...),
    video_max_bitrate: int = Form(...),
    audio_codec: str = Form(...),
    audio_def_language: str = Form(None),
    audio_languages: str = Form(None),
    subtitle_codec: str = Form(...),
    subtitle_def_language: str = Form(None),
    subtitle_languages: str = Form(None),
    db: Session = Depends(get_db)
):
    new_profile = models.Profile(
        name=name, video_codec=video_codec, container=container,
        video_max_res=video_max_res, video_max_bitrate=video_max_bitrate,
        audio_codec=audio_codec, audio_def_language=audio_def_language,
        audio_languages=audio_languages, subtitle_codec=subtitle_codec,
        subtitle_def_language=subtitle_def_language, subtitle_languages=subtitle_languages
    )
    db.add(new_profile)
    db.commit()
    ui_logger.debug("Profile created: %s", name)
    return RedirectResponse(url="/profiles", status_code=303)

# --- WORKGIN WITH LIBRARIES ---

@app.get("/libraries", response_class=HTMLResponse)
async def get_libraries(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    libraries = db.query(models.Library).all()
    profiles = db.query(models.Profile).all()
    return render(
        request=request,
        name="libraries.html",
        db=db,
        **stats,
        libraries=libraries,
        profiles=profiles,
    )

@app.post("/libraries")
async def add_library(
    name: str = Form(...),
    media_path: str = Form(...),
    temp_path: str = Form(...),
    profile_id: int = Form(...),
    db: Session = Depends(get_db)
):
    new_library = models.Library(
        name=name,
        media_path=media_path,
        temp_path=temp_path,
        profile_id=profile_id
    )
    db.add(new_library)
    db.commit()
    ui_logger.debug("Library added: %s (%s)", name, media_path)
    return RedirectResponse(url="/libraries", status_code=303)

# --- WORKING WITH QUEUED FILES ---

@app.get("/queue", include_in_schema=False)
async def queue_redirect():
    """Legacy /queue -> Activities -> Queue."""
    return RedirectResponse(url="/activities/queue", status_code=303)


@app.get("/activities/queue", response_class=HTMLResponse)
async def get_queue(
    request: Request,
    status: str = "all",
    db: Session = Depends(get_db),
):
    """Activity -> Queue: one table with status tabs, *arr style."""
    stats = compute_global_stats(db)

    counts = {
        "pending": (
            db.query(models.MediaFile)
            .filter(models.MediaFile.status == "pending")
            .count()
        ),
        "queued": (
            db.query(models.MediaFile)
            .filter(models.MediaFile.status == "queued")
            .count()
        ),
        "processing": (
            db.query(models.MediaFile)
            .filter(models.MediaFile.status == "processing")
            .count()
        ),
        "completed": (
            db.query(models.MediaFile)
            .filter(models.MediaFile.status == "completed")
            .count()
        ),
    }

    valid = {"all", "pending", "queued", "processing", "completed"}
    if status not in valid:
        status = "all"

    query = db.query(models.MediaFile)
    if status == "all":
        query = query.filter(
            models.MediaFile.status.in_(("pending", "queued", "processing", "completed"))
        )
    else:
        query = query.filter(models.MediaFile.status == status)

    entries = query.order_by(models.MediaFile.id.desc()).limit(100).all()

    return render(
        request=request,
        name="queue.html",
        db=db,
        **stats,
        entries=entries,
        counts=counts,
        status=status,
    )


@app.get("/activities/history", response_class=HTMLResponse)
async def activity_history(
    request: Request,
    status: str = "all",
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Activity -> History: processed files (completed/failed), paginated."""
    stats = compute_global_stats(db)

    query = db.query(models.MediaFile).filter(
        models.MediaFile.status.in_(("completed", "failed"))
    )
    if status == "completed":
        query = query.filter(models.MediaFile.status == "completed")
    elif status == "failed":
        query = query.filter(models.MediaFile.status == "failed")
    if q:
        query = query.filter(models.MediaFile.file_name.ilike(f"%{q}%"))

    total = query.count()
    entries = (
        query.order_by(models.MediaFile.finished_at.desc())
        .offset((page - 1) * LOGS_PER_PAGE)
        .limit(LOGS_PER_PAGE)
        .all()
    )
    has_more = (page * LOGS_PER_PAGE) < total

    is_hx = request.headers.get("HX-Request") == "true"
    template_name = "_history_rows.html" if is_hx else "activities_history.html"

    return render(
        request=request,
        name=template_name,
        db=db,
        **stats,
        entries=entries,
        status=status,
        q=q,
        page=page,
        has_more=has_more,
    )

@app.get("/scan")
async def manual_scan(request: Request, db: Session = Depends(get_db)):
    out = tasks.run_scan(db)
    ui_logger.info("Manual scan completed: %s", out["result"])
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)

# --- DELETE PROFILES & LIBRARIES ---

@app.post("/profiles/{profile_id}/delete")
async def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if profile:
        db.delete(profile)
        db.commit()
        ui_logger.info("Profile deleted: id=%s (%s)", profile_id, profile.name)
    return RedirectResponse(url="/profiles", status_code=303)

@app.post("/libraries/{library_id}/delete")
async def delete_library(library_id: int, db: Session = Depends(get_db)):
    library = db.query(models.Library).filter(models.Library.id == library_id).first()
    if library:
        db.delete(library)
        db.commit()
        ui_logger.info("Library deleted: id=%s (%s)", library_id, library.name)
    return RedirectResponse(url="/libraries", status_code=303)

# --- WORKING WITH JOB QUEUE ---

@app.post("/queue/{media_id}/enqueue")
async def enqueue_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if not media or media.status != "pending":
        return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

    # SAFETY: block enqueue if there is any 'und' (human must decide).
    # Fresh ffprobe so the decision never relies on stale scan summary.
    if has_und_language(media, fresh=True):
        ui_logger.warning(
            "Enqueue blocked for media_file id=%s (%s): unknown language (und)",
            media.id,
            media.file_name,
        )
        referer = request.headers.get("referer", "/")
        sep = "&" if "?" in referer else "?"
        return RedirectResponse(
            url=f"{referer}{sep}error=und_blocked",
            status_code=303,
        )

    media.status = "queued"
    db.commit()
    ui_logger.info("Enqueued media_file id=%s (%s)", media.id, media.file_name)

    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)

@app.post("/queue/{media_id}/dequeue")
async def dequeue_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if media and media.status == "queued":
        media.status = "pending"
        db.commit()
        ui_logger.info("Dequeued media_file id=%s (%s)", media.id, media.file_name)
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)

@app.post("/queue/{media_id}/rescan")
async def rescan_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = (db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first())
    if media and media.status in ("completed", "failed"):
        # Allow re-processing of completed files AND retry of failed files.
        # Failed files go back to 'pending' so the user can review (e.g.
        # last_error) before enqueueing again.
        media.status = "pending"
        media.started_at = None
        media.finished_at = None
        media.job_plan = None
        media.verification_result = None
        media.last_error = None
        media.warnings = None
        db.commit()
        ui_logger.info("Rescanned media_file id=%s (%s) -> pending", media.id, media.file_name)

    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)

# --- WORKING WITH BATCH WORKS BY LIBRARIE ---

# --- COUNTING FILES ---
@app.get("/libraries/{library_id}/enqueue/preview")
async def preview_enqueue_library(library_id: int, db: Session = Depends(get_db)):
    count = (
        db.query(func.count(models.MediaFile.id))
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status == "pending",
        )
        .scalar()
    )

    return {"affected_files": count}

@app.get("/libraries/{library_id}/dequeue/preview")
async def preview_dequeue_library(library_id: int, db: Session = Depends(get_db)):
    count = (
        db.query(func.count(models.MediaFile.id))
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status == "queued",
        )
        .scalar()
    )

    return {"affected_files": count}

@app.get("/libraries/{library_id}/rescan/preview")
async def preview_rescan_library(library_id: int, db: Session = Depends(get_db)):
    count = (
        db.query(func.count(models.MediaFile.id))
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status.in_(("completed", "failed")),
        )
        .scalar()
    )

    return {"affected_files": count}

# --- BATCH UPDATES ---

@app.post("/libraries/{library_id}/enqueue")
async def enqueue_library(
    library_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    media_files = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status == "pending",
        )
        .all()
    )

    enqueued = 0
    blocked = 0
    for media in media_files:
        # SAFETY: skip files with 'und' (human must decide).
        # Fresh probe only when the stored summary is missing (failed scan).
        needs_fresh = (
            media.audio_languages is None and media.subtitle_languages is None
        )
        if has_und_language(media, fresh=needs_fresh):
            blocked += 1
            continue

        media.status = "queued"
        enqueued += 1

    db.commit()
    ui_logger.info(
        "Batch enqueue library id=%s: %s enqueued, %s blocked (und)",
        library_id,
        enqueued,
        blocked,
    )

    referer = request.headers.get("referer", "/")
    sep = "&" if "?" in referer else "?"
    batch_msg = quote(
        f"Enqueued: {enqueued} | Blocked by unknown language (und): {blocked}"
    )
    return RedirectResponse(
        url=f"{referer}{sep}batch={batch_msg}",
        status_code=303,
    )

@app.post("/libraries/{library_id}/dequeue")
async def dequeue_library(
    library_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status == "queued",
        )
        .update({models.MediaFile.status: "pending"}, synchronize_session=False)
    )
    db.commit()
    ui_logger.info("Batch dequeue library id=%s", library_id)

    return RedirectResponse(
        url=request.headers.get("referer", "/"),
        status_code=303,
    )

@app.post("/libraries/{library_id}/rescan")
async def rescan_library(
    library_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status.in_(("completed", "failed")),
        )
        .update(
            {
                models.MediaFile.status: "pending",
                models.MediaFile.started_at: None,
                models.MediaFile.finished_at: None,
                models.MediaFile.job_plan: None,
                models.MediaFile.verification_result: None,
                models.MediaFile.last_error: None,
                models.MediaFile.warnings: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    ui_logger.info("Batch rescan library id=%s -> pending", library_id)

    return RedirectResponse(
        url=request.headers.get("referer", "/"),
        status_code=303,
    )

# -------------------------------------------------
# GLOBAL SEARCH (topbar)
# -------------------------------------------------

@app.get("/api/search")
async def global_search(
    q: str = "",
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """Search media files by file name (title, quality, languages...)."""
    q = q.strip()
    if not q:
        return {"results": []}

    results = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.file_name.ilike(f"%{q}%"))
        .order_by(models.MediaFile.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "results": [
            {
                "id": m.id,
                "file_name": m.file_name,
                "library": m.library.name if m.library else "",
                "status": m.status,
            }
            for m in results
        ]
    }


# --- DASHBOARD POLLING API ---

@app.get("/api/media/status")
async def get_media_status(db: Session = Depends(get_db)):
    """
    Lightweight endpoint used by the dashboard polling logic.
    Returns the current status of all media files.
    """
    results = (
        db.query(models.MediaFile.id, models.MediaFile.status)
        .all()
    )

    return [
        {
            "id": media_id,
            "status": status,
        }
        for media_id, status in results
    ]

# --- DASHBOARD ROW DATA POLLING API ---

@app.get("/api/media/{media_id}/row")
async def get_media_row(media_id: int, db: Session = Depends(get_db)):
    """
    Returns the full set of dashboard-visible fields for a single MediaFile.
    Used to refresh a table row when processing is completed.
    """
    media = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.id == media_id)
        .first()
    )

    if not media:
        return {}

    return {
        "id": media.id,
        "status": media.status,

        "video_codec": media.video_codec,
        "resolution": media.resolution,

        "audio_codec": media.audio_codec,
        "audio_languages": media.audio_languages,

        "subtitle_codec": media.subtitle_codec,
        "subtitle_languages": media.subtitle_languages,

        # Size formatted exactly like the dashboard (MB, rounded)
        "size_final_mb": (
            round(media.size_final / 1048576)
            if media.size_final is not None
            else None
        ),
    }

# --- DASHBOARD STATS POLLING API ---

@app.get("/api/dashboard/stats")
async def get_dashboard_stats(db: Session = Depends(get_db)):
    """
    Lightweight endpoint used by the dashboard polling logic.
    Returns global storage statistics as JSON.
    """
    return compute_global_stats(db)
    
# ---------------------------------------------------------
# FILE SYSTEM BROWSING (DOCKER CONTAINER ONLY)
# ---------------------------------------------------------

ALLOWED_BASE_PATHS = {
    "media": "/media",
    "temp": "/temp",
}


def list_directories(base_key: str, path: str | None = None):
    """
    Safely list directories inside allowed base paths.
    This function never allows escaping the allowed base directory.
    """

    base_path = ALLOWED_BASE_PATHS[base_key]
    current_path = path or base_path

    real_base_path = os.path.realpath(base_path)
    real_current_path = os.path.realpath(current_path)

    # Security check: never allow escaping the base directory
    if not real_current_path.startswith(real_base_path):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isdir(real_current_path):
        raise HTTPException(status_code=404, detail="Directory not found")

    directories = []

    try:
        for entry in os.listdir(real_current_path):
            full_path = os.path.join(real_current_path, entry)
            if os.path.isdir(full_path):
                directories.append({
                    "name": entry,
                    "path": full_path,
                })
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to read directory")

    return {
        "current_path": real_current_path,
        "directories": directories,
    }


@app.get("/api/browse/media")
async def browse_media(path: str | None = Query(default=None)):
    """
    Browse directories inside /media
    """
    return list_directories("media", path)


@app.get("/api/browse/temp")
async def browse_temp(path: str | None = Query(default=None)):
    """
    Browse directories inside /temp
    """
    return list_directories("temp", path)

# -------------------------------------------------
# FFPROBE RAW METADATA (UI / MANUAL INSPECTION)
# -------------------------------------------------

@app.get("/api/media/{media_id}/ffprobe")
async def get_media_ffprobe(media_id: int, db: Session = Depends(get_db)):
    """
    Returns full ffprobe JSON output for a media file.
    Used for manual stream inspection and overrides.
    """

    media = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.id == media_id)
        .first()
    )

    if not media:
        raise HTTPException(status_code=404, detail="Media file not found")

    if not os.path.exists(media.full_path):
        raise HTTPException(status_code=404, detail="Media file not found on disk")

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        media.full_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )

        return json.loads(result.stdout)

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"ffprobe failed: {exc}",
        )

# -------------------------------------------------
# STREAM LANGUAGE OVERRIDES (UI / MANUAL EDITING)
# -------------------------------------------------

@app.get("/api/media/{media_id}/stream-overrides")
async def get_stream_overrides(media_id: int, db: Session = Depends(get_db)):
    """
    Returns saved per-stream language overrides for a media file.
    """
    media = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.id == media_id)
        .first()
    )

    if not media:
        raise HTTPException(status_code=404, detail="Media file not found")

    if not media.stream_overrides:
        return {}

    try:
        return json.loads(media.stream_overrides)
    except Exception:
        # If data is corrupted, do not crash the UI
        return {}

@app.post("/api/media/{media_id}/stream-overrides")
def save_stream_overrides(
    media_id: int,
    payload: Optional[Dict] = Body(None),
    db: Session = Depends(get_db),
):
    """
    Saves per-stream language overrides as JSON string in the database.
    Expected payload format:
    {
      "audio": { "1": "spa", "2": "eng" },
      "subtitle": { "5": "spa" }
    }
    """
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    # Clear = NULL
    if payload is None:
        media.stream_overrides = None
    else:
        media.stream_overrides = json.dumps(payload)

    db.commit()
    ui_logger.debug("Stream overrides saved for media_file id=%s", media_id)
    return {"ok": True}

def _contains_und(value: str | None) -> bool:
    """
    Returns True if a comma-separated language summary contains 'und'.
    """
    if not value:
        return False
    return any(lang.strip() == "und" for lang in value.split(","))


def has_und_language(media: models.MediaFile, fresh: bool = False) -> bool:
    """
    Returns True if there is any 'und' language in audio or subtitles,
    taking stream_overrides into account.

    fresh=True performs a real ffprobe call instead of relying on the
    summary fields stored at scan time (which may be stale or missing).
    If ffprobe fails entirely we are conservative: the file is considered
    'undetermined' and a human must decide.
    """

    # 1. If overrides exist, they have priority
    if media.stream_overrides:
        try:
            overrides = json.loads(media.stream_overrides)

            for lang in overrides.get("audio", {}).values():
                if lang == "und":
                    return True

            for lang in overrides.get("subtitle", {}).values():
                if lang == "und":
                    return True

            # Overrides exist and none is 'und'
            return False

        except Exception:
            # If overrides are broken, be conservative
            return True

    # 2. Source of truth: fresh ffprobe (individual enqueue) or stored summary (batch)
    if fresh:
        meta = get_video_metadata(media.full_path)

        # ffprobe failed entirely (no video info either) -> cannot determine
        # -> human must decide
        if (
            meta.get("video_codec") is None
            and meta.get("audio_languages") is None
            and meta.get("subtitle_languages") is None
        ):
            return True

        return _contains_und(meta.get("audio_languages")) or _contains_und(
            meta.get("subtitle_languages")
        )

    return _contains_und(media.audio_languages) or _contains_und(
        media.subtitle_languages
    )


# -------------------------------------------------
# SYSTEM: LOGS (*arr-style, System -> Logs)
# -------------------------------------------------

LOGS_PER_PAGE = 50


@app.get("/system/logs", response_class=HTMLResponse)
async def system_logs(
    request: Request,
    level: str = "all",
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    """System -> Logs: paginated table with level filter and text search."""
    stats = compute_global_stats(db)

    query = db.query(models.Log)
    if level != "all":
        if level == "error":
            query = query.filter(models.Log.level.in_(("ERROR", "FATAL")))
        else:
            query = query.filter(models.Log.level == level.upper())
    if q:
        query = query.filter(models.Log.message.ilike(f"%{q}%"))

    total = query.count()
    logs = (
        query.order_by(models.Log.id.desc())
        .offset((page - 1) * LOGS_PER_PAGE)
        .limit(LOGS_PER_PAGE)
        .all()
    )
    has_more = (page * LOGS_PER_PAGE) < total

    # HTMX "load more" requests get only the rows fragment
    is_hx = request.headers.get("HX-Request") == "true"
    template_name = "_log_rows.html" if is_hx else "system_logs.html"

    return render(
        request=request,
        name=template_name,
        db=db,
        **stats,
        logs=logs,
        level=level,
        q=q,
        page=page,
        has_more=has_more,
    )


# -------------------------------------------------
# SYSTEM: LOG FILES (*arr-style, System -> Log Files)
# -------------------------------------------------

_LOG_FILE_RE = re.compile(r"^thresherr(\.debug|\.trace)?\.txt(\.\d+)?$")
_VIEW_TAIL_BYTES = 256 * 1024


def _safe_log_file_name(file_name: str) -> str:
    """Validate a log file name; never allow paths or arbitrary files."""
    if not _LOG_FILE_RE.fullmatch(file_name or ""):
        raise HTTPException(status_code=400, detail="Invalid log file name")
    return file_name


def _list_log_files() -> list[dict]:
    files = []
    for path in glob.glob(os.path.join(LOG_DIR, "thresherr*.txt*")):
        st = os.stat(path)
        files.append(
            {
                "name": os.path.basename(path),
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        )
    files.sort(key=lambda f: f["name"])
    return files


def _read_tail(path: str, max_bytes: int) -> str:
    """Read the last max_bytes of a file (log files can be huge)."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= max_bytes:
            data = f.read()
        else:
            f.seek(size - max_bytes)
            data = f.read()
    return data.decode("utf-8", errors="replace")


@app.get("/system/logfiles", response_class=HTMLResponse)
async def system_logfiles(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    fmt_dt = _make_fmt_dt(db)
    files = [
        {**f, "mtime_str": fmt_dt(datetime.fromtimestamp(f["mtime"]))}
        for f in _list_log_files()
    ]
    return render(
        request=request,
        name="system_logfiles.html",
        db=db,
        **stats,
        files=files,
        log_dir=LOG_DIR,
    )


@app.get("/system/logfiles/view/{file_name}", response_class=HTMLResponse)
async def view_log_file(file_name: str, request: Request, db: Session = Depends(get_db)):
    name = _safe_log_file_name(file_name)
    path = os.path.join(LOG_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found")

    stats = compute_global_stats(db)
    return render(
        request=request,
        name="system_logview.html",
        db=db,
        **stats,
        file_name=name,
        content=_read_tail(path, _VIEW_TAIL_BYTES),
        truncated=os.path.getsize(path) > _VIEW_TAIL_BYTES,
    )


@app.get("/system/logfiles/download/{file_name}")
async def download_log_file(file_name: str):
    name = _safe_log_file_name(file_name)
    path = os.path.join(LOG_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(path, media_type="text/plain", filename=name)


@app.post("/system/logfiles/delete/{file_name}")
async def delete_log_file(file_name: str):
    name = _safe_log_file_name(file_name)
    path = os.path.join(LOG_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Log file not found")
    os.remove(path)
    ui_logger.info("Log file deleted: %s", name)
    return RedirectResponse(url="/system/logfiles", status_code=303)


# -------------------------------------------------
# SYSTEM: STATUS & HEALTH (*arr-style, System -> Status)
# -------------------------------------------------
# General info (versions/uptime/DB/worker) + storage usage + health checks.
# Health checks report PROBLEMS only; an empty list means everything is fine
# (same philosophy as Radarr/Sonarr System -> Status).

@app.get("/system/status", response_class=HTMLResponse)
async def system_status(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    fmt_dt = _make_fmt_dt(db)
    ws = health.worker_state(db)
    uptime_seconds = (
        datetime.now(timezone.utc).replace(tzinfo=None) - APP_START_TIME
    ).total_seconds()
    return render(
        request=request,
        name="system_status.html",
        db=db,
        **stats,
        app_version=APP_VERSION,
        python_version=health.python_version(),
        sqlite_version=health.sqlite_version(),
        db_info=health.db_info(DB_PATH),
        uptime=health.format_duration(uptime_seconds),
        worker=ws,
        worker_last_seen=fmt_dt(ws["last_seen"]) if ws["last_seen"] else None,
        worker_started=fmt_dt(ws["started_at"]) if ws["started_at"] else None,
        storage=health.storage_info(),
        health_issues=health.run_health_checks(db, DB_PATH),
        fmt_bytes=health.format_bytes,
    )


# -------------------------------------------------
# SYSTEM: TASKS (*arr-style, System -> Tasks)
# -------------------------------------------------
# Scheduled tasks: Scan Libraries (interval configurable), Recycle Bin
# Cleanup and Backups. The worker runs them in its ~hourly housekeeping
# block; the Execute buttons (and the API) run them immediately. Every run
# records its outcome in the settings table for the Tasks page.

@app.get("/system/tasks", response_class=HTMLResponse)
async def system_tasks(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    fmt_dt = _make_fmt_dt(db)
    return render(
        request=request,
        name="system_tasks.html",
        db=db,
        **stats,
        scan_state=tasks.scan_state(db),
        recycle_state=tasks.recycle_state(db),
        backup_state=tasks.backup_state(db),
        worker=health.worker_state(db),
        fmt_dt=fmt_dt,
    )


@app.post("/system/tasks/scan/execute")
async def execute_scan_task(db: Session = Depends(get_db)):
    out = tasks.run_scan(db)
    ui_logger.info("Task scan executed: %s", out["result"])
    return RedirectResponse(
        url=f"/system/tasks?batch={quote(out['result'])}", status_code=303
    )


@app.post("/system/tasks/recycle/execute")
async def execute_recycle_task(db: Session = Depends(get_db)):
    out = tasks.run_recycle_cleanup(db)
    ui_logger.info("Task recycle executed: %s", out["result"])
    return RedirectResponse(
        url=f"/system/tasks?batch={quote(out['result'])}", status_code=303
    )


@app.post("/system/tasks/backup/execute")
async def execute_backup_task(db: Session = Depends(get_db)):
    try:
        out = _run_backup_now(db)
        result = f"backup created: {os.path.basename(out['path'])}"
    except Exception as exc:
        ui_logger.error("Task backup failed: %s", exc)
        result = f"error: {exc}"
    ui_logger.info("Task backup executed: %s", result)
    return RedirectResponse(
        url=f"/system/tasks?batch={quote(result)}", status_code=303
    )


@app.post("/system/tasks/scan/config")
async def save_scan_config(
    scan_interval_hours: str = Form("0"),
    db: Session = Depends(get_db),
):
    try:
        interval = max(0, int(scan_interval_hours))
    except ValueError:
        interval = 0
    _set_setting(db, "scan_interval_hours", str(interval))
    db.commit()
    ui_logger.info("Scan interval set to %s hour(s)", interval)
    return RedirectResponse(url="/system/tasks?saved=1", status_code=303)


# -------------------------------------------------
# SYSTEM: BACKUPS (*arr-style, System -> Backups)
# -------------------------------------------------
# Backups are consistent SQLite snapshots (Online Backup API) packed as
# timestamped ZIPs in /data/backups. Restore is STAGED: the validated
# database is left as thresherr.db.pending_restore and applied on the next
# startup (see database._apply_pending_restore), so a stack restart is
# required to complete a restore. This avoids replacing a live WAL database
# behind running processes.

@app.get("/system/backups", response_class=HTMLResponse)
async def system_backups(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    fmt_dt = _make_fmt_dt(db)
    backup_list = backups.list_backups(backups.BACKUP_DIR)
    for b in backup_list:
        b["time_str"] = fmt_dt(datetime.fromtimestamp(b["mtime"]))
        b["size_str"] = backups.format_size(b["size"])
    return render(
        request=request,
        name="system_backups.html",
        db=db,
        **stats,
        backups=backup_list,
        backup_dir=backups.BACKUP_DIR,
        pending_restore=backups.pending_restore_exists(DB_PATH),
        interval_days=_get_setting(db, "backup_interval_days", "7"),
        retention=_get_setting(db, "backup_retention", "7"),
    )


@app.post("/system/backups/create")
async def create_backup_route(db: Session = Depends(get_db)):
    try:
        _run_backup_now(db)
        return RedirectResponse(url="/system/backups?backup=ok", status_code=303)
    except Exception as exc:
        ui_logger.error("Backup creation failed: %s", exc)
        return RedirectResponse(
            url="/system/backups?error=backup_failed", status_code=303
        )


def _run_backup_now(db: Session) -> dict:
    """Create a backup and enforce retention; shared by UI and API."""
    path = backups.create_backup(DB_PATH, backups.BACKUP_DIR)
    try:
        retention = int(_get_setting(db, "backup_retention", "7") or "7")
    except ValueError:
        retention = 7
    removed = backups.enforce_retention(backups.BACKUP_DIR, retention)
    ui_logger.info(
        "Backup created: %s (retention removed %s)",
        os.path.basename(path),
        removed,
    )
    return {"path": path, "removed": removed}


@app.post("/system/backups/settings")
async def save_backup_settings(
    backup_interval_days: str = Form("7"),
    backup_retention: str = Form("7"),
    db: Session = Depends(get_db),
):
    try:
        interval = max(0, int(backup_interval_days))
    except ValueError:
        interval = 7
    try:
        retention = max(1, int(backup_retention))
    except ValueError:
        retention = 7
    _set_setting(db, "backup_interval_days", str(interval))
    _set_setting(db, "backup_retention", str(retention))
    db.commit()
    ui_logger.info(
        "Backup settings saved (interval=%s days, retention=%s)",
        interval,
        retention,
    )
    return RedirectResponse(url="/system/backups?saved=ok", status_code=303)


@app.get("/system/backups/{name}/download")
async def download_backup_route(name: str):
    path = backups.safe_backup_path(backups.BACKUP_DIR, name)
    if not path:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/zip", filename=name)


@app.post("/system/backups/{name}/restore")
async def restore_backup_route(name: str):
    try:
        pending = backups.stage_restore(backups.BACKUP_DIR, name, DB_PATH)
    except ValueError as exc:
        ui_logger.error("Restore rejected for %s: %s", name, exc)
        return RedirectResponse(
            url="/system/backups?error=restore_failed", status_code=303
        )
    ui_logger.warning(
        "Restore staged from %s -> %s (stack restart required)",
        name,
        pending,
    )
    return RedirectResponse(url="/system/backups?restore=ok", status_code=303)


@app.post("/system/backups/{name}/delete")
async def delete_backup_route(name: str):
    if not backups.delete_backup(backups.BACKUP_DIR, name):
        raise HTTPException(status_code=404, detail="Backup not found")
    ui_logger.info("Backup deleted: %s", name)
    return RedirectResponse(url="/system/backups?delete=ok", status_code=303)


# -------------------------------------------------
# SETTINGS: GENERAL (Log level, like the *arr family)
# -------------------------------------------------

@app.get("/settings", include_in_schema=False)
async def settings_redirect():
    """Legacy /settings -> General."""
    return RedirectResponse(url="/settings/general", status_code=303)


# -------------------------------------------------
# Settings: GENERAL (log level + security)
# -------------------------------------------------

@app.get("/settings/general", response_class=HTMLResponse)
async def get_settings_general(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    return render(
        request=request,
        name="settings_general.html",
        db=db,
        **stats,
        log_level=get_log_level(),
        log_dir=LOG_DIR,
        api_key=get_api_key(db),
        auth_enabled_setting=_get_setting(db, "auth_enabled", "0"),
        auth_username=_get_setting(db, "auth_username", ""),
    )


@app.post("/settings/general")
async def save_settings_general(
    log_level: str = Form(...),
    auth_enabled: str = Form(None),
    auth_username: str = Form(""),
    auth_password: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        applied = set_log_level(log_level)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid log level")
    ui_logger.info("Log level changed to %s (runtime, no restart)", applied)

    # Authentication (*arr style)
    username = auth_username.strip()
    _set_setting(db, "auth_username", username)
    if auth_password:
        _set_setting(db, "auth_password_hash", hash_password(auth_password))
        ui_logger.info("Auth password updated")

    has_credentials = bool(username) and bool(
        _get_setting(db, "auth_password_hash", "")
    )
    want_auth = auth_enabled == "1"
    if want_auth and has_credentials:
        _set_setting(db, "auth_enabled", "1")
        ui_logger.info("UI authentication ENABLED")
    else:
        _set_setting(db, "auth_enabled", "0")
        if want_auth and not has_credentials:
            ui_logger.warning(
                "Auth enable requested but username/password missing; keeping auth off"
            )
    db.commit()
    return RedirectResponse(url="/settings/general", status_code=303)


# -------------------------------------------------
# Login / Logout
# -------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str = "",
    error: str = "",
):
    # Already authenticated? Go straight to the app
    db = SessionLocal()
    try:
        if _current_user(db, request) is not None:
            return RedirectResponse(url=next or "/", status_code=303)
    finally:
        db.close()
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": next if next.startswith("/") else "",
            "error": error,
            "t": i18n.translator(i18n.DEFAULT_LANGUAGE),
        },
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    db = SessionLocal()
    try:
        stored_hash = _get_setting(db, "auth_password_hash", "") or ""
        expected_user = _get_setting(db, "auth_username", "") or ""
        if (
            _get_setting(db, "auth_enabled", "0") == "1"
            and username == expected_user
            and stored_hash
            and verify_password(password, stored_hash)
        ):
            token = create_session(db)
            ui_logger.info("User logged in: %s", username)
            resp = RedirectResponse(
                url=next if next.startswith("/") else "/", status_code=303
            )
            resp.set_cookie(
                SESSION_COOKIE,
                token,
                max_age=SESSION_DAYS * 86400,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return resp
    finally:
        db.close()

    ui_logger.warning("Failed login attempt for user: %s", username)
    return RedirectResponse(url="/login?error=invalid", status_code=303)


@app.post("/logout")
async def logout_submit(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db = SessionLocal()
        try:
            delete_session(db, token)
        finally:
            db.close()
    ui_logger.info("User logged out")
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/settings/api-key/reset")
async def reset_api_key_route(db: Session = Depends(get_db)):
    reset_api_key(db)
    return RedirectResponse(url="/settings/general", status_code=303)


# -------------------------------------------------
# Settings: MEDIA MANAGEMENT (naming + recycle bin)
# -------------------------------------------------

@app.get("/settings/media-management", response_class=HTMLResponse)
async def get_settings_media_management(
    request: Request, db: Session = Depends(get_db)
):
    stats = compute_global_stats(db)
    recycle_path = _get_setting(db, "recycle_bin_path", "/data/recycle_bin") or ""
    return render(
        request=request,
        name="settings_media_management.html",
        db=db,
        **stats,
        naming_enabled=_get_setting(db, "naming_enabled", "0"),
        naming_template=_get_setting(db, "naming_template", ""),
        recycle_enabled=_get_setting(db, "recycle_bin_enabled", "0"),
        recycle_path=recycle_path,
        recycle_days=_get_setting(db, "recycle_bin_days", "7"),
        recycle_stats=_recycle_stats(recycle_path),
    )


@app.post("/settings/media-management")
async def save_settings_media_management(
    naming_enabled: str = Form(None),
    naming_template: str = Form(""),
    recycle_bin_enabled: str = Form(None),
    recycle_bin_path: str = Form(""),
    recycle_bin_days: str = Form("7"),
    db: Session = Depends(get_db),
):
    _set_setting(db, "naming_enabled", "1" if naming_enabled else "0")
    _set_setting(db, "naming_template", naming_template)
    ui_logger.info(
        "Naming config saved (enabled=%s, template=%s)",
        bool(naming_enabled),
        naming_template,
    )

    try:
        recycle_days_int = max(1, int(recycle_bin_days))
    except ValueError:
        recycle_days_int = 7
    _set_setting(db, "recycle_bin_enabled", "1" if recycle_bin_enabled else "0")
    _set_setting(db, "recycle_bin_path", recycle_bin_path.strip())
    _set_setting(db, "recycle_bin_days", str(recycle_days_int))
    ui_logger.info(
        "Recycle bin config saved (enabled=%s, path=%s, days=%s)",
        bool(recycle_bin_enabled),
        recycle_bin_path.strip(),
        recycle_days_int,
    )
    db.commit()
    return RedirectResponse(url="/settings/media-management", status_code=303)


@app.get("/settings/naming/preview")
async def naming_preview(template: str = ""):
    """Live preview for the naming template, using a sample movie."""
    name = naming.build_output_name(
        template=template,
        title="Matrix Revolutions",
        year="2003",
        quality="1080p",
        video_codec="h264",
        audio_codecs="E-AC3",
        audio_languages="ES-EN",
        subtitle_languages="ES",
        container="mkv",
    )
    return {"name": name}


# -------------------------------------------------
# Settings: INTERFACE (language + date/time formats)
# -------------------------------------------------

@app.get("/settings/interfaz", response_class=HTMLResponse)
async def get_settings_interfaz(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    return render(
        request=request,
        name="settings_interfaz.html",
        db=db,
        **stats,
        date_format=_get_setting(db, "date_format", "iso"),
        time_format=_get_setting(db, "time_format", "24"),
        week_start=_get_setting(db, "week_start", "monday"),
    )


@app.post("/settings/interfaz")
async def save_settings_interfaz(
    ui_language: str = Form(None),
    date_format: str = Form("iso"),
    time_format: str = Form("24"),
    week_start: str = Form("monday"),
    db: Session = Depends(get_db),
):
    if ui_language and i18n.is_valid_language(ui_language):
        _set_setting(db, "ui_language", ui_language)
        ui_logger.info("UI language changed to %s (runtime, no restart)", ui_language)

    if date_format in DATE_FORMATS:
        _set_setting(db, "date_format", date_format)
    if time_format in TIME_FORMATS:
        _set_setting(db, "time_format", time_format)
    if week_start in ("monday", "sunday"):
        _set_setting(db, "week_start", week_start)
    ui_logger.info(
        "Interface settings saved (lang=%s, date=%s, time=%s, week=%s)",
        ui_language,
        date_format,
        time_format,
        week_start,
    )
    db.commit()
    return RedirectResponse(url="/settings/interfaz", status_code=303)


@app.post("/settings")
async def save_settings(
    log_level: str = Form(...),
    ui_language: str = Form(None),
    db: Session = Depends(get_db),
):
    try:
        applied = set_log_level(log_level)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid log level")
    ui_logger.info("Log level changed to %s (runtime, no restart)", applied)

    if ui_language and i18n.is_valid_language(ui_language):
        setting = (
            db.query(models.Setting)
            .filter(models.Setting.key == "ui_language")
            .first()
        )
        if setting:
            setting.value = ui_language
        else:
            db.add(models.Setting(key="ui_language", value=ui_language))
        db.commit()
        ui_logger.info("UI language changed to %s (runtime, no restart)", ui_language)

    return RedirectResponse(url="/settings", status_code=303)

# Ensure an API key exists from the very first run (Settings -> General -> Security)
_bootstrap_db = SessionLocal()
try:
    get_api_key(_bootstrap_db)
finally:
    _bootstrap_db.close()
