from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

# 1. Imports (Absolute imports for Docker)
import models
import database
from database import engine, SessionLocal

# 2. Database setup (Create tables)
models.Base.metadata.create_all(bind=engine)

# 3. App & Templates initialization
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 4. Dependency (Defined BEFORE the routes)
def get_db():
    """
    Dependency to provide a database session to endpoints.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 5. Routes
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
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
    request: Request,
    name: str = Form(...),
    video_codec: str = Form(...),
    container: str = Form(...),
    video_max_res: int = Form(...),
    video_max_bitrate: int = Form(...),
    audio_languages: str = Form(...),
    db: Session = Depends(get_db)
):
    # Logic to save the new profile
    new_profile = models.Profile(
        name=name,
        video_codec=video_codec,
        container=container,
        video_max_res=video_max_res,
        video_max_bitrate=video_max_bitrate,
        audio_languages=audio_languages
    )
    db.add(new_profile)
    db.commit()
    
    # Return a fragment for HTMX
    return f"<div class='p-3 bg-slate-700 rounded border border-slate-600 mb-2'>{name} ({video_codec})</div>"
