import os
import subprocess
import json
from sqlalchemy.orm import Session
import models

# Common video extensions
VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v')

def get_video_metadata(file_path):
    """
    Uses ffprobe to extract technical details from the video file.
    All comments in English as requested.
    """
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", file_path
    ]
    try:
        # Run ffprobe and capture output
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
            # Get language tag, default to 'und' (undefined)
            lang = stream.get("tags", {}).get("language", "und")

            if stype == "video" and not info["v_codec"]:
                info["v_codec"] = codec
                info["res"] = f"{stream.get('width')}x{stream.get('height')}"
            elif stype == "audio":
                info["a_codec"].add(codec)
                info["a_langs"].add(lang)
            elif stype == "subtitle":
                info["s_codec"].add(codec)
                info["s_langs"].add(lang)

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
            "video_codec": "unknown", 
            "resolution": "---",
            "audio_codec": "n/a", 
            "audio_languages": "und"
        }

def scan_libraries(db: Session):
    """
    Scans paths and populates the database with new files and their metadata.
    """
    libraries = db.query(models.Library).all()
    new_files_count = 0

    for lib in libraries:
        if not os.path.exists(lib.media_path):
            print(f"Path not found: {lib.media_path}")
            continue

        for root, dirs, files in os.walk(lib.media_path):
            for file in files:
                if file.lower().endswith(VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    
                    # Avoid duplicates
                    exists = db.query(models.MediaFile).filter(models.MediaFile.full_path == full_path).first()
                    
                    if not exists:
                        # Extract metadata using ffprobe
                        meta = get_video_metadata(full_path)
                        
                        new_media = models.MediaFile(
                            file_name=file,
                            full_path=full_path,
                            library_id=lib.id,
                            status="pending",
                            size_original=os.path.getsize(full_path),
                            **meta # Injects video_codec, resolution, etc.
                        )
                        db.add(new_media)
                        new_files_count += 1
    
    db.commit()
    return new_files_count
