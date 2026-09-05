import os
import re
import subprocess
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.orm import Session
import models
import naming
from logging_setup import get_logger

logger = get_logger("scanner")

# Common video extensions
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm")

# Number of parallel ffprobe workers during a library scan. Probing is
# I/O-bound (local disk or CIFS/NAS reads), so 4 workers are safe even on
# modest CPUs. Override with the THRESHERR_SCAN_WORKERS env var when needed.
PROBE_WORKERS = int(os.environ.get("THRESHERR_SCAN_WORKERS", "4"))


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
        video_bitrate = None
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
                try:
                    video_bitrate = int(stream["bit_rate"])
                except (KeyError, TypeError, ValueError):
                    video_bitrate = None
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
            "video_bitrate": video_bitrate,
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
            "video_bitrate": None,
            "audio_codec": None,
            "audio_languages": None,
            "subtitle_codec": None,
            "subtitle_languages": None,
        }


# -------------------------------------------------
# Library scan
# -------------------------------------------------

def _probe_one(full_path: str):
    """
    ffprobe a single file (runs inside the scan thread pool).

    Returns (full_path, meta, size) on success, or (full_path, None, None)
    when the file is unreadable. Never raises: a single broken file must
    not abort the whole scan.
    """
    try:
        meta = get_video_metadata(full_path)
        size = os.path.getsize(full_path)
        return full_path, meta, size
    except OSError as exc:
        logger.warning("Skipping unreadable file %s: %s", full_path, exc)
        return full_path, None, None


def scan_libraries(db: Session, batch_size: int = 250,
                   progress=None, workers: int | None = None) -> int:
    """
    Discover media files and register them in the database.

    IMPORTANT:
    - This function ONLY discovers files
    - Status is always set to 'pending'
    - No processing decisions are made here

    Concurrency notes:
    - ffprobe calls run in a small thread pool (I/O-bound, GIL released by
      subprocess), while ALL database writes stay on the calling thread
      (SQLAlchemy sessions are not thread-safe).
    - Inserts are committed in batches (default 250) instead of one giant
      commit per library, so the SQLite write lock is only held for
      milliseconds and the worker/UI can keep writing during a long scan.

    progress: optional callable(done, total) invoked periodically from the
    calling thread while files are being probed.
    """
    libraries = db.query(models.Library).all()
    new_files_count = 0
    probe_workers = workers if workers is not None else PROBE_WORKERS

    for library in libraries:
        if not os.path.exists(library.media_path):
            logger.warning("Library media path missing: %s", library.media_path)
            continue

        # Load existing paths once (no N+1 per file)
        existing = {
            row[0]
            for row in db.query(models.MediaFile.full_path)
            .filter(models.MediaFile.library_id == library.id)
            .all()
        }

        # Single inventory pass: we only walk the tree once and collect the
        # files that need probing. This also gives us the total upfront so
        # the UI can show real progress (done/total).
        to_probe = []
        for root, _, files in os.walk(library.media_path):
            for file in files:
                if not file.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                full_path = os.path.join(root, file)
                if full_path in existing:
                    continue
                to_probe.append(full_path)

        total = len(to_probe)
        if total == 0:
            continue

        logger.info(
            "Scanning library %s: %s new file(s) with %s probe worker(s)",
            library.name, total, probe_workers,
        )

        done = 0
        added_since_commit = 0

        def _notify_progress() -> None:
            if progress is None:
                return
            # Throttle: every 25 files and always on the last one.
            if done % 25 == 0 or done == total:
                try:
                    progress(done, total)
                except Exception:
                    logger.warning("Scan progress callback failed", exc_info=True)

        with ThreadPoolExecutor(max_workers=probe_workers) as pool:
            futures = [pool.submit(_probe_one, fp) for fp in to_probe]
            for future in as_completed(futures):
                done += 1
                try:
                    full_path, meta, size = future.result()
                except Exception as exc:
                    # Defensive: a probe must not abort the whole scan.
                    logger.warning("Scan probe crashed: %s", exc)
                    _notify_progress()
                    continue

                if meta is None:
                    _notify_progress()
                    continue

                media = models.MediaFile(
                    file_name=os.path.basename(full_path),
                    full_path=full_path,
                    library_id=library.id,
                    status="pending",
                    size_original=size,
                    **meta,
                )

                db.add(media)
                existing.add(full_path)
                new_files_count += 1
                added_since_commit += 1

                if added_since_commit >= batch_size:
                    db.commit()
                    added_since_commit = 0

                _notify_progress()

        db.commit()

    return new_files_count
