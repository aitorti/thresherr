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
    
# HTML Fragment for HTMX
    html_content = f"""
    <div class="bg-slate-900 p-4 rounded-lg border border-slate-700 shadow-inner">
        <h3 class="font-bold text-amber-400 mb-2 border-b border-slate-800 pb-1">{name}</h3>
        <div class="grid grid-cols-2 gap-2 text-xs text-slate-400">
            <p>🎥 <span class="text-slate-200">{video_codec.upper()} / {video_max_res}p</span></p>
            <p>📦 <span class="text-slate-200">{container.upper()}</span></p>
            <p>🔊 <span class="text-slate-200">{audio_codec.upper()} ({audio_def_language or 'N/A'}{f', {audio_languages}' if audio_languages else ''})</span></p>
            <p>📝 <span class="text-slate-200">{subtitle_codec.upper()} ({subtitle_def_language or 'N/A'}{f', {subtitle_languages}' if subtitle_languages else ''})</span></p>
        </div>
    </div>
    <div id="empty-msg" hx-swap-oob="delete"></div>
    """
    
    return HTMLResponse(content=html_content, status_code=200)

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
async def create_library(
    name: str = Form(...),
    media_path: str = Form(...),
    temp_path: str = Form(...),
    profile_id: int = Form(...),
    db: Session = Depends(get_db)
):
    new_lib = models.Library(
        name=name, 
        media_path=media_path, 
        temp_path=temp_path, 
        profile_id=profile_id
    )
    db.add(new_lib)
    db.commit()
    db.refresh(new_lib)
    
    # Fragmento HTML para HTMX con el nombre del perfil incluido
    html_content = f"""
    <div class="bg-slate-900 p-4 rounded-lg border border-slate-700 flex justify-between items-center shadow-inner">
        <div>
            <h3 class="font-bold text-amber-400">{name}</h3>
            <p class="text-xs text-slate-500 mt-1 italic">{media_path}</p>
        </div>
        <div class="text-right">
            <span class="text-xs bg-slate-800 px-2 py-1 rounded border border-slate-600 text-slate-300">
                Target: {new_lib.profile.name}
            </span>
        </div>
    </div>
    <div id="empty-lib-msg" hx-swap-oob="delete"></div>
    """
    return HTMLResponse(content=html_content)
