import os
import re
import subprocess
import json
import unicodedata
from datetime import datetime, timezone
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

# Summary fields refreshed from a fresh ffprobe. This is UI/cache data only;
# the worker NEVER trusts it for processing decisions.
SUMMARY_FIELDS = (
    "video_codec",
    "resolution",
    "video_bitrate",
    "audio_codec",
    "audio_languages",
    "subtitle_codec",
    "subtitle_languages",
)


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp, coherent with the rest of the codebase."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

def was_processed(media) -> bool:
    """True when the row already went through the worker.

    For such a row full_path now points at the TRANSCODED OUTPUT, so:
    - size_original holds the pre-transcode SOURCE size (savings accounting)
    - size_final holds the size of the file currently on disk
    Neither of them may be overwritten by a fresh probe.
    """
    return media.status == "completed" or media.size_final is not None


def apply_fresh_metadata(media, meta: dict, size: int | None) -> None:
    """Refresh the summary fields of a MediaFile from a fresh ffprobe result.

    SAFETY RULE 1: a field is only written when the fresh probe returned a
    value. An existing value is NEVER overwritten with None, so a partial or
    failed probe can never erase a cached card.

    SAFETY RULE 2: size_original is only rewritten for a row that was NEVER
    processed. For a processed row it is the historical source size used by
    the savings accounting (size_original - size_final); overwriting it with
    the output size destroys the stats.
    """
    for field in SUMMARY_FIELDS:
        value = meta.get(field)
        if value is None:
            continue
        setattr(media, field, value)
    if size is not None and not was_processed(media):
        media.size_original = size


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
                   progress=None, workers: int | None = None) -> tuple[int, int]:
    """
    Discover media files and register them in the database.

    Two kinds of work happen here:
    - brand-new files are inserted with status='pending' (as before);
    - files already registered whose on-disk size NO LONGER matches the
      stored size_original were REPLACED in the same path: their summary card
      is refreshed from a fresh ffprobe. Status is never touched: whether to
      re-process a replaced file stays a human decision.

    Returns (new_files_count, refreshed_files_count).

    IMPORTANT:
    - This function ONLY discovers files and refreshes stale summaries
    - Status is always set to 'pending' for NEW files (never changed here)
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
    refreshed_files_count = 0
    probe_workers = workers if workers is not None else PROBE_WORKERS

    for library in libraries:
        if not os.path.exists(library.media_path):
            logger.warning("Library media path missing: %s", library.media_path)
            continue

        # Load existing rows once (no N+1 per file):
        # full_path -> (id, size_original, size_final, status)
        # size_final/status pick the right reference size: for an already
        # processed row, size_original is the pre-transcode SOURCE size, not
        # the size of the file currently on disk.
        existing = {
            row.full_path: (row.id, row.size_original, row.size_final, row.status)
            for row in db.query(
                models.MediaFile.id,
                models.MediaFile.full_path,
                models.MediaFile.size_original,
                models.MediaFile.size_final,
                models.MediaFile.status,
            )
            .filter(models.MediaFile.library_id == library.id)
            .all()
        }

        # Single inventory pass: walk the tree once and collect the files
        # that need probing. New files AND files whose on-disk size differs
        # from the recorded size_original (replaced in place) are probed, so
        # the UI still gets a real total upfront for the progress bar.
        to_probe = []  # list of (full_path, existing_row_id | None)
        for root, _, files in os.walk(library.media_path):
            for file in files:
                if not file.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                full_path = os.path.join(root, file)
                known = existing.get(full_path)
                if known is None:
                    to_probe.append((full_path, None))
                    continue
                row_id, old_size, final_size, status = known
                try:
                    disk_size = os.path.getsize(full_path)
                except OSError as exc:
                    logger.warning("Cannot stat known file %s: %s", full_path, exc)
                    continue
                # Reference size for THIS path. A processed row is compared
                # against size_final (the file on disk is the output); a row
                # never processed against size_original. Comparing a processed
                # row against size_original would flag EVERY completed file as
                # "replaced" and wipe the savings accounting.
                reference = final_size if status == "completed" else old_size
                # NULL reference means the cached card cannot be trusted:
                # treat it as replaced so the summary is rebuilt.
                if reference is None or disk_size != reference:
                    to_probe.append((full_path, row_id))

        total = len(to_probe)
        if total == 0:
            continue

        logger.info(
            "Scanning library %s: %s file(s) to probe with %s worker(s)",
            library.name, total, probe_workers,
        )

        done = 0
        pending_writes = 0

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
            futures = {
                pool.submit(_probe_one, full_path): (full_path, row_id)
                for full_path, row_id in to_probe
            }
            for future in as_completed(futures):
                done += 1
                full_path, row_id = futures[future]
                try:
                    _, meta, size = future.result()
                except Exception as exc:
                    # Defensive: a probe must not abort the whole scan.
                    logger.warning("Scan probe crashed: %s", exc)
                    _notify_progress()
                    continue

                if meta is None:
                    _notify_progress()
                    continue

                if row_id is not None:
                    # Known path, size changed: the file was REPLACED in
                    # place. Refresh the summary card only; status and the
                    # processing decision are deliberately left untouched.
                    media = db.get(models.MediaFile, row_id)
                    if media is None:
                        _notify_progress()
                        continue
                    if not any(meta.get(f) is not None for f in SUMMARY_FIELDS):
                        # ffprobe gave nothing usable: keep the old card.
                        logger.warning(
                            "ffprobe returned no usable metadata for replaced "
                            "file %s; media_file id=%s left untouched",
                            full_path, row_id,
                        )
                        _notify_progress()
                        continue
                    apply_fresh_metadata(media, meta, size)
                    media.source_changed_at = _utcnow_naive()
                    refreshed_files_count += 1
                    logger.info(
                        "File replaced in place, refreshed card: id=%s (%s)",
                        media.id, full_path,
                    )
                else:
                    media = models.MediaFile(
                        file_name=os.path.basename(full_path),
                        full_path=full_path,
                        library_id=library.id,
                        status="pending",
                        size_original=size,
                        **meta,
                    )
                    db.add(media)
                    existing[full_path] = (None, size)
                    new_files_count += 1

                pending_writes += 1
                if pending_writes >= batch_size:
                    db.commit()
                    pending_writes = 0

                _notify_progress()

        db.commit()

    return new_files_count, refreshed_files_count
