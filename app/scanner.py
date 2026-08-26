import os
import re
import subprocess
import json
import unicodedata
from sqlalchemy.orm import Session
import models
import naming
from logging_setup import get_logger

logger = get_logger("scanner")

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

# -------------------------------------------------
# Language inference helpers (shared)
# -------------------------------------------------

def _normalize_text(value: str) -> str:
    """
    Normalize text for robust keyword matching:
    - Lowercase
    - Remove accents (NFKD)
    - Collapse whitespace
    """
    if not value:
        return ""
    value = value.strip().lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"\s+", " ", value)
    return value


def _is_unknown_language(lang: str) -> bool:
    """
    Returns True if a language tag is missing or effectively 'undetermined'.
    """
    if not lang:
        return True
    lang = lang.strip().lower()
    return lang in {"und", "undetermined", "unknown", "undefined", "none", "null", "-"}


def _map_to_iso639_2(lang: str) -> str:
    """
    Normalize common 2-letter codes to ISO-639-2 (3-letter) when possible.
    Keeps unknown values unchanged.
    """
    if not lang:
        return "und"
    lang = lang.strip().lower()

    # Common 2-letter → 3-letter mappings (extend as needed)
    mapping = {
        "en": "eng",
        "es": "spa",
        "esp": "spa",
        "fr": "fra",
        "it": "ita",
        "de": "deu",
        "pt": "por",
        "ja": "jpn",
        "zh": "chi",
        "ru": "rus",
        "nl": "nld",
    }
    return mapping.get(lang, lang)


def infer_stream_language(tags: dict) -> str:
    """
    Infer a canonical language code for a stream using:
    1) tags.language (primary)
    2) tags.title (fallback if language is missing/und)

    Special handling:
    - Distinguish 'spa' vs 'latam' using LATAM keywords (title-based).
    """
    
    # Normalize tag keys to lowercase (ffprobe may return LANGUAGE, TITLE, etc.)
    tags = {k.lower(): v for k, v in tags.items()}

    raw_lang = (tags.get("language") or "")
    raw_title = (tags.get("title") or "")

    lang = _map_to_iso639_2(_normalize_text(raw_lang))
    title = _normalize_text(clean_stream_title(raw_title))

    # Keywords that indicate Latin American Spanish variants
    latam_keywords = {
        "latam", "latino", "latin", "latin american", "latinoamericano",
        "america", "americano", "mexico", "argentina", "colombia", "chile",
        "peru", "venezuela", "ecuador", "uruguay", "paraguay", "bolivia",
    }

    # If language is known and Spanish-like, apply spa/latam refinement
    if not _is_unknown_language(lang):
        if lang in {"spa", "es", "esp"}:
            return "latam" if any(k in title for k in latam_keywords) else "spa"
        return lang

    # Fallback: infer from title keywords
    # Keep this small and opinionated; extend based on your library
    title_map = {
        "spa": ["castellano", "espanol", "español", "spanish"],
        "eng": ["ingles", "inglés", "english", "eng", "vo", "original"],
        "fra": ["frances", "français", "french", "vff", "vfq"],
        "ita": ["italiano", "italian"],
        "deu": ["aleman", "alemán", "german", "deutsch"],
        "por": ["portugues", "portugués", "portuguese", "por"],
    }

    inferred = "und"
    for iso, keywords in title_map.items():
        if any(k in title for k in keywords):
            inferred = iso
            break

    if inferred == "spa":
        return "latam" if any(k in title for k in latam_keywords) else "spa"

    return inferred

def refine_spanish_language(tags: dict) -> str:
    """
    Backwards-compatible wrapper.
    """
    return infer_stream_language(tags)

def get_resolution_name(height: int | None) -> str:
    """Backwards-compatible wrapper (deprecated: height-only)."""
    return naming.quality_from_dimensions(None, height) or "Unknown"

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
        audio_languages = []
        subtitle_codecs = set()
        subtitle_languages = []

        for stream in data.get("streams", []):
            stype = stream.get("codec_type")
            codec = stream.get("codec_name", "unknown")
            tags = stream.get("tags") or {}

            if stype == "video" and not video_codec:
                video_codec = codec
                # Commercial tier from BOTH dimensions (letterbox-safe):
                # a 1920x800 scope release is 1080p, not 720p.
                resolution = naming.quality_from_dimensions(
                    stream.get("width"), stream.get("height")
                )

            elif stype == "audio":
                audio_codecs.add(codec)
                lang = infer_stream_language(tags)
                if lang not in audio_languages:
                    audio_languages.append(lang)

            elif stype == "subtitle":
                subtitle_codecs.add(codec)
                lang = infer_stream_language(tags)
                if lang not in subtitle_languages:
                    subtitle_languages.append(lang)

        return {
            "video_codec": video_codec,
            "resolution": resolution,
            "audio_codec": ", ".join(sorted(audio_codecs)) if audio_codecs else None,
            "audio_languages": ", ".join(audio_languages) if audio_languages else None,
            "subtitle_codec": ", ".join(sorted(subtitle_codecs)) if subtitle_codecs else None,
            "subtitle_languages": ", ".join(subtitle_languages) if subtitle_languages else None,
        }

    except Exception as exc:
        logger.warning("ffprobe failed for %s: %s", file_path, exc)
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
