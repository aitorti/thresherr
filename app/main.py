from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi import APIRouter
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import engine, SessionLocal
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

import models
import subprocess
import json
import os
import glob
import re
import time
import hmac
import secrets
from datetime import datetime
from urllib.parse import quote

APP_VERSION = "0.1.0"

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


def render(request: Request, name: str, db: Session, **context) -> HTMLResponse:
    """
    Render a template with the *arr-style i18n helpers injected:
    - t(key, **kwargs): translate for the current UI language
    - lang: current language code
    - languages: available languages (code -> native name)
    """
    lang = _ui_language(db)
    context["t"] = i18n.translator(lang)
    context["lang"] = lang
    context["languages"] = i18n.LANGUAGES
    return templates.TemplateResponse(request=request, name=name, context=context)


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
    """Lightweight status endpoint, the first piece of the public API."""
    return {
        "app": "thresherr",
        "version": APP_VERSION,
        "stats": compute_global_stats(db),
    }


app.include_router(api_v1)

# --- LOADING GLOBAL STATISTICS ---

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
async def dashboard(request: Request, db: Session = Depends(get_db)):
    # Global statistics (shared with sidebar)
    stats = compute_global_stats(db)

    # THIS LINE IS CRITICAL:
    media_files = db.query(models.MediaFile).order_by(models.MediaFile.id.desc()).all()
    
    for mf in media_files:
        mf.has_stream_overrides = mf.stream_overrides is not None


    return render(
        request=request,
        name="dashboard.html",
        db=db,
        **stats,
        media_files=media_files,
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

@app.get("/queue", response_class=HTMLResponse)
async def get_queue(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    pending = db.query(models.MediaFile).filter(models.MediaFile.status == "pending").all()
    queued = db.query(models.MediaFile).filter(models.MediaFile.status == "queued").all()
    processing = db.query(models.MediaFile).filter(models.MediaFile.status == "processing").all()
    completed = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.status == "completed")
        .order_by(models.MediaFile.id.desc())
        .limit(10)
        .all()
    )
    return render(
        request=request,
        name="queue.html",
        db=db,
        **stats,
        pending=pending,
        queued=queued,
        processing=processing,
        completed=completed,
    )

@app.get("/scan")
async def manual_scan(request: Request, db: Session = Depends(get_db)):
    new_count = scan_libraries(db)
    ui_logger.info("Manual scan completed: %s new media file(s)", new_count)
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


def _format_log_time(dt) -> str:
    """Render a naive-UTC Log row in Europe/Madrid, *arr-style table format."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as dt_timezone

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S")


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
        fmt_log_time=_format_log_time,
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
                "mtime_str": _format_log_time(
                    datetime.fromtimestamp(st.st_mtime)
                ),
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
    return render(
        request=request,
        name="system_logfiles.html",
        db=db,
        **stats,
        files=_list_log_files(),
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
# SETTINGS: GENERAL (Log level, like the *arr family)
# -------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def get_settings(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    return render(
        request=request,
        name="settings.html",
        db=db,
        **stats,
        log_level=get_log_level(),
        log_dir=LOG_DIR,
        api_key=get_api_key(db),
    )


@app.post("/settings/api-key/reset")
async def reset_api_key_route(db: Session = Depends(get_db)):
    reset_api_key(db)
    return RedirectResponse(url="/settings", status_code=303)


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
