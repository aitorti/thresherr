from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import models
from database import engine, SessionLocal

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
async def dashboard(request: Request):
    # Fixed: Passing request as the first argument as required by modern Starlette
    return templates.TemplateResponse(
        request=request,
        name="base.html"
    )

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
