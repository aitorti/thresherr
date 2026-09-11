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

# Fields whose value can be inferred (the language pass) and must therefore
# never be *downgraded* by a plain refresh.
LANGUAGE_FIELDS = ("audio_languages", "subtitle_languages")

# Version of the summary card: the extractor + classifier that produced it.
# Bump it whenever either changes; the next scan then re-reads every card
# stamped with a different value, once, automatically. That is how a rule
# change reaches the whole library without re-probing everything every scan.
SUMMARY_VERSION = 1

# Two mtimes closer than this are considered the same file. Filesystems (and
# especially the CIFS share) round mtimes, so an exact comparison would flag
# files that were merely copied around.
MTIME_TOLERANCE = 1.0

# Safety rails for the "entry whose file no longer exists" reconciliation.
# Removing rows is irreversible, and a dead CIFS mount looks exactly like
# "someone deleted the whole library". Under REMOVAL_MIN_ROWS, or at or below
# REMOVAL_MAX_FRACTION of a library, a wave of missing files is treated as a
# broken scan and nothing is removed.
REMOVAL_MIN_ROWS = 5
REMOVAL_MAX_FRACTION = 0.25


def removal_looks_dangerous(total: int, missing: int) -> bool:
    """True when removing `missing` of `total` rows looks like a broken scan."""
    if total <= 0 or missing < REMOVAL_MIN_ROWS:
        return False
    return (missing / total) > REMOVAL_MAX_FRACTION


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

def was_processed_row(status: str | None, size_final: int | None) -> bool:
    """True when a row already went through the worker.

    Decided on the DATA, never on the status alone: a rescan sets a processed
    row back to 'pending' while the file on disk is still the transcoded
    output. Single source of truth for that question.
    """
    return status == "completed" or size_final is not None


def was_processed(media) -> bool:
    """True when the row already went through the worker.

    For such a row full_path now points at the TRANSCODED OUTPUT, so:
    - size_original holds the pre-transcode SOURCE size (savings accounting)
    - size_final holds the size of the file currently on disk
    Neither of them may be overwritten by a fresh probe.
    """
    return was_processed_row(media.status, media.size_final)


def _merge_languages(stored: str | None, fresh: str | None) -> str | None:
    """Union of the languages we already knew and the ones the probe reports.

    A probe answers 'und' when the file simply does not say (untagged subtitle
    tracks, for instance). That is the ABSENCE of information, not a new
    value: replacing a resolved 'spa' with 'und' throws away what the language
    pass worked out, and the next scan resolves it again, for ever.
    """
    def split(value):
        return [part.strip() for part in (value or "").split(",") if part.strip()]

    known = [part for part in split(stored) if part != "und"]
    for part in split(fresh):
        if part != "und" and part not in known:
            known.append(part)
    if known:
        return ", ".join(known)
    return fresh


def apply_fresh_metadata(media, meta: dict, size: int | None,
                         mtime: float | None = None,
                         replace_languages: bool = False) -> None:
    """Refresh the summary fields of a MediaFile from a fresh ffprobe result.

    SAFETY RULE 1: a field is only written when the fresh probe returned a
    value. An existing value is NEVER overwritten with None, so a partial or
    failed probe can never erase a cached card.

    SAFETY RULE 2: size_original is only rewritten for a row that was NEVER
    processed. For a processed row it is the historical source size used by
    the savings accounting (size_original - size_final); overwriting it with
    the output size destroys the stats.

    SAFETY RULE 3: the languages are MERGED, never replaced, unless the caller
    knows the file itself changed (replace_languages=True). A refresh must not
    undo what the language pass resolved.
    """
    for field in SUMMARY_FIELDS:
        value = meta.get(field)
        if value is None:
            continue
        if not replace_languages and field in LANGUAGE_FIELDS:
            value = _merge_languages(getattr(media, field), value)
        setattr(media, field, value)
    if size is not None and not was_processed(media):
        media.size_original = size
    # observed_mtime always tracks the file we just looked at, whatever the
    # row state: it is the reference for the next scan's change detection.
    # Unlike size_original it is safe to refresh for processed rows too.
    if mtime is not None:
        media.observed_mtime = mtime
    # The card was just rebuilt with the current extractor/classifier.
    media.summary_version = SUMMARY_VERSION


def _probe_one(full_path: str):
    """
    ffprobe a single file (runs inside the scan thread pool).

    Returns (full_path, meta, size, mtime) on success, or
    (full_path, None, None, None) when the file is unreadable. Never raises:
    a single broken file must not abort the whole scan.
    """
    try:
        meta = get_video_metadata(full_path)
        st = os.stat(full_path)
        return full_path, meta, st.st_size, st.st_mtime
    except OSError as exc:
        logger.warning("Skipping unreadable file %s: %s", full_path, exc)
        return full_path, None, None, None


def clear_stale_scanning(db: Session) -> int:
    """Send rows stuck in the transient 'scanning' badge back to 'pending'.

    'scanning' only exists while the language cascade runs, and the scan lock
    (settings.scan_running) guarantees a single scan at a time, so any row
    still carrying it when a scan starts is debris from a scan that died
    (crash, kill, container restart). Without this the row keeps being listed
    as "working" for ever and never re-enters the queue. The worker does the
    same on boot, but that only happens on a restart.
    """
    moved = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.status == "scanning")
        .update({models.MediaFile.status: "pending"}, synchronize_session=False)
    )
    db.commit()
    return moved


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

    Returns a dict of counters: new, replaced, reread, backfilled, removed,
    unreadable, unreadable_new.

    - replaced counts files that really CHANGED on disk; reread counts cards
      re-read only because an older extractor/classifier produced them (see
      SUMMARY_VERSION).
    - unreadable counts entries whose file cannot be read at all (corrupt or
      truncated container); they keep the marker and are retried on every scan.
      unreadable_new counts the ones flagged for the first time.

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
    reread_files_count = 0
    backfilled_count = 0
    removed_count = 0
    unreadable_count = 0
    new_unreadable_count = 0
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
            row.full_path: (
                row.id, row.size_original, row.size_final, row.status,
                row.observed_mtime, row.summary_version,
            )
            for row in db.query(
                models.MediaFile.id,
                models.MediaFile.full_path,
                models.MediaFile.size_original,
                models.MediaFile.size_final,
                models.MediaFile.status,
                models.MediaFile.observed_mtime,
                models.MediaFile.summary_version,
            )
            .filter(models.MediaFile.library_id == library.id)
            .all()
        }

        # Single inventory pass: walk the tree once and collect the files
        # that need probing. New files AND files whose on-disk size differs
        # from the recorded size_original (replaced in place) are probed, so
        # the UI still gets a real total upfront for the progress bar.
        to_probe = []  # list of (full_path, row_id | None, file_changed)
        to_backfill = []  # legacy rows (observed_mtime NULL): (row_id, mtime)
        seen = set()  # every video file found on disk in this library
        walk_errors = []  # os.walk failures: the inventory cannot be trusted

        def _on_walk_error(exc) -> None:
            walk_errors.append(exc)
        for root, _, files in os.walk(library.media_path, onerror=_on_walk_error):
            for file in files:
                if not file.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                full_path = os.path.join(root, file)
                seen.add(full_path)
                known = existing.get(full_path)
                if known is None:
                    to_probe.append((full_path, None, False))
                    continue
                row_id, old_size, final_size, status, obs_mtime, card_version = known
                try:
                    st = os.stat(full_path)
                except OSError as exc:
                    logger.warning("Cannot stat known file %s: %s", full_path, exc)
                    continue
                disk_size, disk_mtime = st.st_size, st.st_mtime
                # Reference size for THIS path, decided on the DATA (see
                # was_processed_row): a processed row is compared against
                # size_final, because the file on disk is its output. Using
                # "status == completed" alone was wrong: a rescan sends a
                # processed row back to 'pending' while the file on disk is
                # still the output, and every scan re-flagged it as replaced.
                # A NULL reference means the card cannot be trusted: treat it
                # as replaced so the summary is rebuilt.
                if was_processed_row(status, final_size):
                    # No size_final to compare against: rely on the mtime.
                    size_changed = (
                        final_size is not None and disk_size != final_size
                    )
                else:
                    size_changed = old_size is None or disk_size != old_size
                # mtime hardening: catches a replacement that kept the same
                # size. Only usable with a reference; legacy rows have NULL and
                # are backfilled below (NOT treated as changed).
                mtime_changed = (
                    obs_mtime is not None
                    and abs(disk_mtime - obs_mtime) > MTIME_TOLERANCE
                )
                # Card produced by an older extractor/classifier: re-read it
                # once. This is how a rule change reaches the whole library
                # without re-probing everything on every scan.
                stale_card = card_version != SUMMARY_VERSION
                if stale_card or size_changed or mtime_changed:
                    to_probe.append(
                        (full_path, row_id, size_changed or mtime_changed)
                    )
                elif obs_mtime is None:
                    # Legacy row: record the reference mtime without touching
                    # the card, so the next scan can use mtime detection.
                    to_backfill.append((row_id, disk_mtime))

        done = 0
        pending_writes = 0

        # Legacy rows (observed_mtime IS NULL): record the reference mtime
        # without touching the card or the status. Runs EVEN when the library
        # has nothing to probe, so mtime coverage is backfilled on the first
        # scan after the column was introduced.
        for row_id, disk_mtime in to_backfill:
            media = db.get(models.MediaFile, row_id)
            if media is None or media.observed_mtime is not None:
                continue
            media.observed_mtime = disk_mtime
            backfilled_count += 1
            pending_writes += 1
            if pending_writes >= batch_size:
                db.commit()
                pending_writes = 0
        db.commit()

        # --- Reconciliation: entries whose file is GONE from disk ----------
        # The database must mirror the disk: an entry whose file no longer
        # exists (deleted, moved, or replaced by another release) is removed,
        # whatever the reason and whatever the row status. Guards: a scan that
        # cannot be trusted never deletes anything.
        missing = [fp for fp in existing if fp not in seen]
        if missing and walk_errors:
            logger.warning(
                "Library %s: %s entrie(s) not found on disk, but the walk "
                "reported an error (%s). Nothing removed.",
                library.name, len(missing), walk_errors[0],
            )
        elif missing and removal_looks_dangerous(len(existing), len(missing)):
            logger.warning(
                "Library %s: %s of %s entrie(s) look missing at once; refusing "
                "to remove them (is the share still mounted?). Nothing removed.",
                library.name, len(missing), len(existing),
            )
        elif missing:
            for missing_path in missing:
                media = db.get(models.MediaFile, existing[missing_path][0])
                if media is None:
                    continue
                logger.info(
                    "File no longer on disk, removing entry: id=%s (%s)",
                    media.id, missing_path,
                )
                db.delete(media)
                removed_count += 1
                pending_writes += 1
                if pending_writes >= batch_size:
                    db.commit()
                    pending_writes = 0
            db.commit()

        total = len(to_probe)
        if total == 0:
            continue

        logger.info(
            "Scanning library %s: %s file(s) to probe with %s worker(s)",
            library.name, total, probe_workers,
        )

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
                pool.submit(_probe_one, full_path): (full_path, row_id, changed)
                for full_path, row_id, changed in to_probe
            }
            for future in as_completed(futures):
                done += 1
                full_path, row_id, file_changed = futures[future]
                try:
                    _, meta, size, mtime = future.result()
                except Exception as exc:
                    # Defensive: a probe must not abort the whole scan.
                    logger.warning("Scan probe crashed: %s", exc)
                    _notify_progress()
                    continue

                if meta is None:
                    _notify_progress()
                    continue

                usable = any(meta.get(f) is not None for f in SUMMARY_FIELDS)

                if row_id is not None:
                    # Known path. Refresh the summary card only; status and the
                    # processing decision are deliberately left untouched.
                    media = db.get(models.MediaFile, row_id)
                    if media is None:
                        _notify_progress()
                        continue
                    if not usable:
                        # ffprobe could not read the file at all: keep the old
                        # card and flag the entry so the UI can show it. The
                        # card is NOT stamped, so the next scan retries it.
                        if media.unreadable_at is None:
                            media.unreadable_at = _utcnow_naive()
                            new_unreadable_count += 1
                        unreadable_count += 1
                        logger.warning(
                            "Unreadable file (probe failed): id=%s (%s)",
                            media.id, full_path,
                        )
                        _notify_progress()
                        continue
                    if media.unreadable_at is not None:
                        media.unreadable_at = None  # readable again
                    apply_fresh_metadata(
                        media, meta, size, mtime, replace_languages=file_changed
                    )
                    # Only a file that really changed on disk deserves the
                    # badge: a version-driven re-read must not flag anything.
                    if file_changed:
                        media.source_changed_at = _utcnow_naive()
                        refreshed_files_count += 1
                        logger.info(
                            "File replaced in place, refreshed card: id=%s (%s)",
                            media.id, full_path,
                        )
                    else:
                        reread_files_count += 1
                        logger.info(
                            "Card re-read (older rules): id=%s (%s)",
                            media.id, full_path,
                        )
                else:
                    # A brand-new file that cannot be read is STILL registered
                    # and flagged, so the UI can show it: an invisible corrupt
                    # file helps nobody. Leaving the card unstamped keeps it in
                    # the retry path.
                    media = models.MediaFile(
                        file_name=os.path.basename(full_path),
                        full_path=full_path,
                        library_id=library.id,
                        status="pending",
                        size_original=size,
                        observed_mtime=mtime,
                        summary_version=SUMMARY_VERSION if usable else None,
                        unreadable_at=None if usable else _utcnow_naive(),
                        **meta,
                    )
                    db.add(media)
                    existing[full_path] = (
                        None, size, None, "pending", mtime,
                        media.summary_version,
                    )
                    new_files_count += 1
                    if not usable:
                        unreadable_count += 1
                        new_unreadable_count += 1
                        logger.warning(
                            "New unreadable file (probe failed): %s", full_path
                        )

                pending_writes += 1
                if pending_writes >= batch_size:
                    db.commit()
                    pending_writes = 0

                _notify_progress()

        db.commit()

    return {
        "new": new_files_count,
        "replaced": refreshed_files_count,
        "reread": reread_files_count,
        "backfilled": backfilled_count,
        "removed": removed_count,
        "unreadable": unreadable_count,
        "unreadable_new": new_unreadable_count,
    }
