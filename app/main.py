from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
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

# 4. Routes

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    # Statistics calculations
    total_orig = db.query(func.sum(models.MediaFile.size_original)).scalar() or 0
    total_done_orig = db.query(func.sum(models.MediaFile.size_original)).filter(models.MediaFile.status == "completed").scalar() or 0
    total_done_final = db.query(func.sum(models.MediaFile.size_final)).filter(models.MediaFile.status == "completed").scalar() or 0
    
    savings = total_done_orig - total_done_final
    savings_pct = (savings / total_done_orig * 100) if total_done_orig > 0 else 0

    # THIS LINE IS CRITICAL:
    media_files = db.query(models.MediaFile).order_by(models.MediaFile.id.desc()).all()

    return templates.TemplateResponse(
        request=request, 
        name="dashboard.html", 
        context={
            "total_gb": round(total_orig / (1024**3), 2),
            "processed_orig_gb": round(total_done_orig / (1024**3), 2),
            "processed_final_gb": round(total_done_final / (1024**3), 2),
            "savings_gb": round(savings / (1024**3), 2),
            "savings_pct": round(savings_pct, 1),
            "media_files": media_files
        }
    )

# --- WORKGIN WITH PROFILES ---

@app.get("/profiles", response_class=HTMLResponse)
async def get_profiles(request: Request, db: Session = Depends(get_db)):
    profiles = db.query(models.Profile).all()
    return templates.TemplateResponse(
        request=request,
        name="profiles.html", 
        context={"profiles": profiles}
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
    libraries = db.query(models.Library).all()
    profiles = db.query(models.Profile).all()
    return templates.TemplateResponse(
        request=request,
        name="libraries.html", 
        context={"libraries": libraries, "profiles": profiles}
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

@app.get("/queue", response_class=HTMLResponse)
async def get_queue(request: Request, db: Session = Depends(get_db)):
    pending = db.query(models.MediaFile).filter(models.MediaFile.status == "pending").all()
    queued = db.query(models.MediaFile).filter(models.MediaFile.status == "queued").all()
    processing = db.query(models.MediaFile).filter(models.MediaFile.status == "processing").all()
    completed = db.query(models.MediaFile).filter(models.MediaFile.status == "completed").order_by(models.MediaFile.id.desc()).limit(10).all()
    
    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "pending": pending,
            "queued": queued,
            "processing": processing,
            "completed": completed
        }
    )

@app.get("/scan")
async def manual_scan(db: Session = Depends(get_db)):
    new_count = scan_libraries(db)
    return RedirectResponse(url="/queue", status_code=303)

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
async def enqueue_media(media_id: int, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if media and media.status == "pending":
        media.status = "queued"
        db.commit()
    return RedirectResponse(url="/queue", status_code=303)

@app.post("/queue/{media_id}/dequeue")
async def dequeue_media(media_id: int, db: Session = Depends(get_db)):
    media = db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first()
    if media and media.status == "queued":
        media.status = "pending"
        db.commit()
    return RedirectResponse(url="/queue", status_code=303)

@app.post("/queue/{media_id}/rescan")
async def rescan_media(media_id: int, db: Session = Depends(get_db)):
    media = (db.query(models.MediaFile).filter(models.MediaFile.id == media_id).first())
    if media and media.status == "completed":
        media.status = "pending"
        media.started_at = None
        media.finished_at = None
        media.job_plan = None
        media.verification_result = None
        media.last_error = None
        db.commit()

    return RedirectResponse(url="/queue", status_code=303)

