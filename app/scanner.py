import os
import subprocess
import json
from sqlalchemy.orm import Session
import models

# Common video extensions
VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v')

def get_resolution_name(height):
    """
    Converts vertical pixels to common resolution names (e.g., 1080p).
    All comments in English as requested.
    """
    if not height: 
        return "Unknown"
    
    if height >= 2160: return "4K"
    if height >= 1440: return "1440p"
    if height >= 1080: return "1080p"
    if height >= 720:  return "720p"
    if height >= 480:  return "480p"
    return f"{height}p"

def get_refined_language(stream_tags):
    """
    Checks language and title tags to distinguish between Spanish (Castellano) and Latino.
    If 'lat', 'latin', or 'latino' is found in the title or lang tag, returns 'latam'.
    """
    # Get language and title, defaulting to empty strings if not present
    lang = stream_tags.get("language", "und").lower()
    title = stream_tags.get("title", "").lower()
    
    # Keywords that indicate Latin American Spanish
    latam_keywords = ["lat", "latin", "latino", "america", "lati"]
    
    # Check if the language is marked as Spanish
    if lang in ["spa", "es", "esp"]:
        # If the title contains any latam keyword, we re-tag it as latam
        if any(key in title for key in latam_keywords):
            return "latam"
        return "spa"
    
    # Also check if the language tag itself is a latam keyword
    if any(key == lang for key in latam_keywords):
        return "latam"
        
    return lang

def get_video_metadata(file_path):
    """
    Uses ffprobe to extract technical details and refines language tags.
    """
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", file_path
    ]
    try:
        # Run ffprobe with a 15-second timeout
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
        
        info = {
            "v_codec": None, "res": None, 
            "a_codec": set(), "a_langs": set(),
            "s_codec": set(), "s_langs": set()
        }
        
        for stream in data.get("streams", []):
            stype = stream.get("codec_type")
            codec = stream.get("codec_name", "unknown")
            tags = stream.get("tags", {})
            
            # Apply the Latino detection logic
            refined_lang = get_refined_language(tags)

            if stype == "video" and not info["v_codec"]:
                info["v_codec"] = codec
                height = stream.get("height")
                info["res"] = get_resolution_name(height)
            elif stype == "audio":
                info["a_codec"].add(codec)
                info["a_langs"].add(refined_lang)
            elif stype == "subtitle":
                info["s_codec"].add(codec)
                info["s_langs"].add(refined_lang)

        return {
            "video_codec": info["v_codec"],
            "resolution": info["res"],
            "audio_codec": ", ".join(info["a_codec"]) if info["a_codec"] else "N/A",
            "audio_languages": ", ".join(info["a_langs"]) if info["a_langs"] else "und",
            "subtitle_codec": ", ".join(info["s_codec"]) if info["s_codec"] else None,
            "subtitle_languages": ", ".join(info["s_langs"]) if info["s_langs"] else None
        }
    except Exception as e:
        print(f"Error probing {file_path}: {e}")
        return {
            "video_codec": "unknown", "resolution": "---",
            "audio_codec": "n/a", "audio_languages": "und"
        }

def scan_libraries(db: Session):
    """
    Scans media paths and populates the database with metadata.
    """
    libraries = db.query(models.Library).all()
    new_files_count = 0

    for lib in libraries:
        if not os.path.exists(lib.media_path):
            continue

        for root, dirs, files in os.walk(lib.media_path):
            for file in files:
                if file.lower().endswith(VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    
                    # Check if file is already in the database
                    exists = db.query(models.MediaFile).filter(models.MediaFile.full_path == full_path).first()
                    
                    if not exists:
                        # Extract metadata with ffprobe and Latino check
                        meta = get_video_metadata(full_path)
                        
                        new_media = models.MediaFile(
                            file_name=file,
                            full_path=full_path,
                            library_id=lib.id,
                            status="pending",
                            size_original=os.path.getsize(full_path),
                            **meta
                        )
                        db.add(new_media)
                        new_files_count += 1
    
    db.commit()
    return new_files_count
