from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

# 1. Imports (Absolute imports for Docker)
import models
import database
from database import engine, SessionLocal

# 2. Database setup (Create tables on startup)
models.Base.metadata.create_all(bind=engine)

# 3. App & Templates initialization
app = FastAPI()
# Inside Docker, the path is just "templates"
templates = Jinja2Templates(directory="templates")

# 4. Dependency to get DB session
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
    audio_codec: str = Form(...),
    audio_def_language: str = Form(None),
    audio_languages: str = Form(None),
    subtitle_codec: str = Form(...),
    subtitle_def_language: str = Form(None),
    subtitle_languages: str = Form(None),
    db: Session = Depends(get_db)
):
    """
    Creates a new profile and returns an HTML fragment for HTMX.
    """
    new_profile = models.Profile(
        name=name,
        video_codec=video_codec,
        container=container,
        video_max_res=video_max_res,
        video_max_bitrate=video_max_bitrate,
        audio_codec=audio_codec,
        audio_def_language=audio_def_language,
        audio_languages=audio_languages,
        subtitle_codec=subtitle_codec,
        subtitle_def_language=subtitle_def_language,
        subtitle_languages=subtitle_languages
    )
    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)
    
    # HTML Fragment for HTMX (no quotes or \n issues)
    html_content = f"""
    <div class="bg-slate-900 p-4 rounded-lg border border-slate-700 shadow-inner">
        <h3 class="font-bold text-amber-400 mb-2 border-b border-slate-800 pb-1">{name}</h3>
        <div class="grid grid-cols-2 gap-2 text-xs text-slate-400">
            <p>🎥 <span class="text-slate-200">{video_codec.upper()} / {video_max_res}p</span></p>
            <p>📦 <span class="text-slate-200">{container.upper()}</span></p>
            <p>🔊 <span class="text-slate-200">{audio_codec.upper()} ({audio_def_language or 'N/A'})</span></p>
            <p>📝 <span class="text-slate-200">{subtitle_codec.upper()}</span></p>
        </div>
    </div>
    """
    
    return HTMLResponse(content=html_content, status_code=200)
