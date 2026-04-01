import os
import re
import subprocess
import json
from sqlalchemy.orm import Session
import models

# Common video extensions
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm")

# -------------------------------------------------
# Helpers (scanner-only, UI oriented)
# -------------------------------------------------

def clean_stream_title(title: str) -> str:
    """
    Removes advertising and unwanted tags from stream titles.
    Used ONLY for better language detection and UI display.
    """
    if not title:
        return ""

    spam_patterns = [
        r"\[.*?\]",           # [bySomeone]
        r"\(.*?\)",           # (www.example.com)
        r"www\..*?\.[a-z]+",  # URLs
        r"@[\w_]+",           # @username
        r"\bby\s+\w+\b",      # by Tony
    ]

    clean = title
    for pattern in spam_patterns:
        clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)

    return clean.strip().lower()


def refine_spanish_language(tags: dict) -> str:
    """
    Distinguish between:
    - spa  -> Castellano
    - latam -> Spanish Latin American

    Logic:
    - Read both 'language' and 'title' tags
    - If any reference to lat/latin/latino/latam appears -> latam
    - Otherwise -> spa
    """

    lang = (tags.get("language") or "").lower()
    title = clean_stream_title(tags.get("title", ""))

    latam_keywords = [
        "lat",
        "latin",
        "latino",
        "latam",
        "latinoamericano",
        "américa",
        "americano",
    ]

    # If language tag explicitly says spa / es
    if lang in {"spa", "es", "esp"}:
        if any(keyword in title for keyword in latam_keywords):
            return "latam"
        return "spa"

    # If language tag itself hints latam
    if any(keyword == lang for keyword in latam_keywords):
        return "latam"

    # Fallback: return original language
    return lang if lang else "und"


def get_resolution_name(height: int | None) -> str:
    if not height:
        return "Unknown"
    if height >= 2160:
        return "2160p"
    if height >= 1440:
        return "1440p"
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return f"{height}p"


# -------------------------------------------------
# Metadata extraction (SUMMARY ONLY)
# -------------------------------------------------

def get_video_metadata(file_path: str) -> dict:
    """
    Uses ffprobe to extract *summary* metadata for UI.
    This data MUST NOT be trusted by the worker.
    """

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )

        data = json.loads(result.stdout)

        video_codec = None
        resolution = None
        audio_codecs = set()
        audio_languages = set()
        subtitle_codecs = set()
        subtitle_languages = set()

        for stream in data.get("streams", []):
            stype = stream.get("codec_type")
            codec = stream.get("codec_name", "unknown")
            tags = stream.get("tags", {})

            if stype == "video" and not video_codec:
                video_codec = codec
                resolution = get_resolution_name(stream.get("height"))

            elif stype == "audio":
                audio_codecs.add(codec)
                lang = refine_spanish_language(tags)
                audio_languages.add(lang)

            elif stype == "subtitle":
                subtitle_codecs.add(codec)
                lang = refine_spanish_language(tags)
                subtitle_languages.add(lang)

        return {
            "video_codec": video_codec,
            "resolution": resolution,
            "audio_codec": ", ".join(sorted(audio_codecs)) if audio_codecs else None,
            "audio_languages": ", ".join(sorted(audio_languages)) if audio_languages else None,
            "subtitle_codec": ", ".join(sorted(subtitle_codecs)) if subtitle_codecs else None,
            "subtitle_languages": ", ".join(sorted(subtitle_languages)) if subtitle_languages else None,
        }

    except Exception as exc:
        print(f"[scanner] ffprobe failed for {file_path}: {exc}")
        return {
            "video_codec": None,
            "resolution": None,
            "audio_codec": None,
            "audio_languages": None,
            "subtitle_codec": None,
            "subtitle_languages": None,
        }


# -------------------------------------------------
# Library scan
# -------------------------------------------------

def scan_libraries(db: Session) -> int:
    """
    Discover media files and register them in the database.

    IMPORTANT:
    - This function ONLY discovers files
    - Status is always set to 'pending'
    - No processing decisions are made here
    """

    libraries = db.query(models.Library).all()
    new_files_count = 0

    for library in libraries:
        if not os.path.exists(library.media_path):
            continue

        for root, _, files in os.walk(library.media_path):
            for file in files:
                if not file.lower().endswith(VIDEO_EXTENSIONS):
                    continue

                full_path = os.path.join(root, file)

                exists = (
                    db.query(models.MediaFile)
                    .filter(models.MediaFile.full_path == full_path)
                    .first()
                )

                if exists:
                    continue

                meta = get_video_metadata(full_path)

                media = models.MediaFile(
                    file_name=file,
                    full_path=full_path,
                    library_id=library.id,
                    status="pending",
                    size_original=os.path.getsize(full_path),
                    **meta,
                )

                db.add(media)
                new_files_count += 1

        db.commit()

    return new_files_count
