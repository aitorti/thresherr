from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
import os
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
import models
from database import engine, SessionLocal
from scanner import scan_libraries

# 1. Database setup
models.Base.metadata.create_all(bind=engine)

# 2. App & Templates initialization
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 3. Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

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

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **stats,
            "media_files": media_files,
        },
    )

# --- WORKGIN WITH PROFILES ---

@app.get("/profiles", response_class=HTMLResponse)
async def get_profiles(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    profiles = db.query(models.Profile).all()
    return templates.TemplateResponse(
        request=request,
        name="profiles.html",
        context={**stats, "profiles": profiles},
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
    return RedirectResponse(url="/profiles", status_code=303)

# --- WORKGIN WITH LIBRARIES ---

@app.get("/libraries", response_class=HTMLResponse)
async def get_libraries(request: Request, db: Session = Depends(get_db)):
    stats = compute_global_stats(db)
    libraries = db.query(models.Library).all()
    profiles = db.query(models.Profile).all()
    return templates.TemplateResponse(
        request=request,
        name="libraries.html",
        context={**stats, "libraries": libraries, "profiles": profiles},
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
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={**stats, "pending": pending, "queued": queued, "processing": processing, "completed": completed},
    )

@app.get("/scan")
async def manual_scan(request: Request, db: Session = Depends(get_db)):
    new_count = scan_libraries(db)
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)

# --- DELETE PROFILES & LIBRARIES ---

@app.post("/profiles/{profile_id}/delete")
async def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if profile:
        db.delete(profile)
        db.commit()
    return RedirectResponse(url="/profiles", status_code=303)

@app.post("/libraries/{library_id}/delete")
async def delete_library(library_id: int, db: Session = Depends(get_db)):
    library = db.query(models.Library).filter(models.Library.id == library_id).first()
    if library:
        db.delete(library)
        db.commit()
    return RedirectResponse(url="/libraries", status_code=303)

# --- WORKING WITH JOB QUEUE ---

@app.post("/queue/{media_id}/enqueue")
async def enqueue_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if media and media.status == "pending":
        media.status = "queued"
        db.commit()

    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)


@app.post("/queue/{media_id}/dequeue")
async def dequeue_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if media and media.status == "queued":
        media.status = "pending"
        db.commit()
    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303,)

@app.post("/queue/{media_id}/rescan")
async def rescan_media(media_id: int, request: Request, db: Session = Depends(get_db)):
    media = (db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first())
    if media and media.status == "completed":
        media.status = "pending"
        media.started_at = None
        media.finished_at = None
        media.job_plan = None
        media.verification_result = None
        media.last_error = None
        db.commit()

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
            models.MediaFile.status == "completed",
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
    (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.library_id == library_id,
            models.MediaFile.status == "pending",
        )
        .update({models.MediaFile.status: "queued"}, synchronize_session=False)
    )
    db.commit()

    return RedirectResponse(
        url=request.headers.get("referer", "/"),
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
            models.MediaFile.status == "completed",
        )
        .update(
            {
                models.MediaFile.status: "pending",
                models.MediaFile.started_at: None,
                models.MediaFile.finished_at: None,
                models.MediaFile.job_plan: None,
                models.MediaFile.verification_result: None,
                models.MediaFile.last_error: None,
            },
            synchronize_session=False,
        )
    )
    db.commit()

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
    