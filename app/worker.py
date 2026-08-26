import os
import time
import json
import subprocess
import shutil
import models

from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from scanner import get_video_metadata
from scanner import infer_stream_language
from database import SessionLocal, engine, DB_PATH
from logging_setup import get_logger, setup_logging
import naming
import backups
import tasks
import settings

logger = get_logger("worker")

# Ensure DB schema exists
models.Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# Worker configuration
# -------------------------------------------------

WORKER_SLEEP_SECONDS = 5

# Jobs stuck in 'processing' for longer than this are considered stale
# (crashed/killed worker) and are automatically re-queued.
STALE_PROCESSING_TIMEOUT_MINUTES = 60

# Heartbeat throttle: the worker writes its liveness to the settings table
# at most once every N seconds (System -> Status reads it). While a job is
# being processed the heartbeat may go stale for the job duration; the
# status page treats a recent 'processing' job as a live worker.
HEARTBEAT_EVERY_SECONDS = 30
_last_heartbeat_write = 0.0


def _utcnow() -> datetime:
    """
    Current UTC time as a naive datetime (SQLite-compatible).
    Replacement for deprecated datetime.utcnow().
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# -------------------------------------------------
# Worker heartbeat (System -> Status)
# -------------------------------------------------

def _set_worker_setting(db, key: str, value: str) -> None:
    settings.set_setting(db, key, value)


def write_heartbeat(db, force: bool = False) -> None:
    """Persist worker liveness; throttled to one write per 30s (idle loop)."""
    global _last_heartbeat_write
    now = time.time()
    if not force and now - _last_heartbeat_write < HEARTBEAT_EVERY_SECONDS:
        return
    _last_heartbeat_write = now
    _set_worker_setting(db, "worker_heartbeat", _utcnow().isoformat())
    db.commit()

# -------------------------------------------------
# Temp output path & cleanup helpers
# -------------------------------------------------

def temp_output_path(full_path: str, temp_dir: str) -> str:
    """
    Path of the temporary output file for a media file.
    Single source of truth for the .thresherr.tmp.mkv naming convention.
    """
    name = os.path.splitext(os.path.basename(full_path))[0]
    return os.path.join(temp_dir, f"{name}.thresherr.tmp.mkv")


def remove_temp_output(job: models.MediaFile) -> None:
    """
    Best-effort removal of the temporary output file for a job.
    Used when a job fails (ffmpeg error, non-compliant output, ...) so that
    orphan .thresherr.tmp.mkv files are never left behind.
    """
    try:
        temp_output = temp_output_path(job.full_path, job.library.temp_path)
        if os.path.exists(temp_output):
            os.remove(temp_output)
            logger.debug("Removed temp output: %s", temp_output)
    except Exception as exc:
        logger.warning("Could not remove temp output %s: %s", temp_output, exc)


# -------------------------------------------------
# Subtitle codec normalization
# -------------------------------------------------

def _normalize_subtitle_codec(codec_name: str | None) -> str | None:
    if not codec_name:
        return None
    c = codec_name.lower()
    if "pgs" in c:
        return "pgs"
    if c in {"subrip", "srt"}:
        return "subrip"
    if "ass" in c:
        return "ass"
    if "webvtt" in c or c == "vtt":
        return "vtt"
    return c

# -------------------------------------------------
# Job claiming
# -------------------------------------------------

def claim_next_job(db: Session) -> models.MediaFile | None:
    """
    Atomically claim the next queued MediaFile.
    Prepared for future multi-worker usage.
    """

    job = (
        db.query(models.MediaFile)
        .filter(models.MediaFile.status == "queued")
        .order_by(models.MediaFile.id.asc())
        .first()
    )

    if not job:
        return None

    job.status = "processing"
    job.started_at = _utcnow()
    db.commit()

    return job


def requeue_stale_processing(db: Session) -> int:
    """
    Re-queue jobs stuck in 'processing' (crashed/killed worker, OOM, ...).

    Only jobs with started_at older than STALE_PROCESSING_TIMEOUT_MINUTES are
    touched, so a legitimately slow job is never re-queued while the worker is
    still working on it (a single worker never claims a second job while one
    is in progress).

    Also cleans up the orphan temp file from the previous attempt, if any.
    """

    cutoff = _utcnow() - timedelta(minutes=STALE_PROCESSING_TIMEOUT_MINUTES)

    stale_jobs = (
        db.query(models.MediaFile)
        .filter(
            models.MediaFile.status == "processing",
            models.MediaFile.started_at.isnot(None),
            models.MediaFile.started_at < cutoff,
        )
        .all()
    )

    for job in stale_jobs:
        # Clean orphan temp file from the previous attempt (if any)
        remove_temp_output(job)

        # Reset work fields so the retry starts clean
        job.status = "queued"
        job.started_at = None
        job.job_plan = None
        job.verification_result = None
        job.last_error = None
        job.warnings = None
        logger.warning("Re-queued stale job id=%s (%s)", job.id, job.file_name)

    if stale_jobs:
        db.commit()

    return len(stale_jobs)

    
# -------------------------------------------------
# Overrides from user
# -------------------------------------------------

def _load_stream_overrides(media: models.MediaFile) -> dict:
    """
    Load per-stream overrides from DB (JSON string) or return {}.
    Expected structure:
    {
      "audio": { "1": "spa", "2": "eng" },
      "subtitle": { "5": "spa" }
    }
    """
    raw = getattr(media, "stream_overrides", None)
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def _apply_language_overrides(streams: list[dict], overrides_map: dict) -> None:
    """
    Apply overrides in-place based on absolute ffprobe stream index.
    Keys may come as strings or ints, normalize to string lookup.
    """
    if not overrides_map:
        return

    normalized = {str(k): v for k, v in overrides_map.items()}

    for s in streams:
        idx = str(s.get("index"))
        if idx in normalized and normalized[idx]:
            s["language"] = normalized[idx]

# -------------------------------------------------
# Inspection
# -------------------------------------------------

def inspect_file(media: models.MediaFile) -> dict:
    """
    Actual inspection of the file using ffprobe (READ-ONLY). 
    Returns an object structure containing audio and subtitle streams. 
    
    Does NOT make decisions, does NOT execute actions, and does NOT touch the database.
    """
    file_path = media.full_path

    cmd = [
        "ffprobe",
        "-v", "error",
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
            timeout=20,
        )
    except FileNotFoundError as exc:
        # ffprobe doesn't exist on the container/host
        raise RuntimeError("ffprobe not found in PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")

    fmt = data.get("format", {}) or {}
    container = fmt.get("format_name")  # ej: "matroska,webm"
    if container and "," in container:
        container = container.split(",")[0].strip()

    # Video, nothing to do for now
    video_info = {"codec": None, "width": None, "height": None, "bitrate": None}

    audio_streams = []
    subtitle_streams = []

    for s in data.get("streams", []) or []:
        stype = s.get("codec_type")
        codec = s.get("codec_name")
        idx = s.get("index")

        tags = s.get("tags", {}) or {}
        lang = infer_stream_language(tags)

        disp = s.get("disposition", {}) or {}
        is_default = bool(disp.get("default", 0))
        is_forced = bool(disp.get("forced", 0))

        if stype == "video" and video_info["codec"] is None:
            video_info = {
                "codec": codec,
                "width": s.get("width"),
                "height": s.get("height"),
                "bitrate": _safe_int(s.get("bit_rate")),
            }

        elif stype == "audio":
            audio_streams.append({
                "index": idx,
                "codec": codec,
                "language": lang,
                "default": is_default,
                "channels": s.get("channels"),
                "sample_rate": _safe_int(s.get("sample_rate")),
                "bitrate": _safe_int(s.get("bit_rate")),
            })

        elif stype == "subtitle":
            subtitle_streams.append({
                "index": idx,
                "codec_raw": codec,
                "codec": _normalize_subtitle_codec(codec),
                "language": lang,
                "default": is_default,
                "forced": is_forced,
            })

    # -------------------------------------------------
    # Apply user stream overrides (language) if present
    # -------------------------------------------------
    overrides = _load_stream_overrides(media)
    _apply_language_overrides(audio_streams, overrides.get("audio", {}))
    _apply_language_overrides(subtitle_streams, overrides.get("subtitle", {}))

    return {
        "container": container,
        "duration": _safe_float(fmt.get("duration")),
        "video": video_info,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
    }


def _safe_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _safe_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

# -------------------------------------------------
# Decide correct audio streams
# -------------------------------------------------

def decide_audio_streams(inspection: dict, profile: models.Profile) -> list:

    """
    Rules for 'und' (unknown language) with "allowed languages" (not required):
    1) Known languages (!= 'und') but NOT allowed -> remove (regardless of codec).
    2) If the file already contains (or can reach) a valid set of audio streams using only
       known+allowed languages (copy or transcode) -> 'und' streams are removable.
    3) If the file contains only a partial set of allowed languages and there are 'und' streams
       with allowed codec or transcodable codec -> DO NOT remove 'und' (they may hide an allowed language).
    4) Never allow the final result to have 0 audio streams.
    """

    def codec_rank_for_target(target: str, codec: str | None) -> int:
        if not codec:
            return 999
        target = (target or "").lower()
        codec = codec.lower()

        preference = {
            "eac3": ["eac3", "ac3", "aac", "dts", "flac", "mp3"],
            "ac3":  ["ac3", "eac3", "aac", "dts", "flac", "mp3"],
            "aac":  ["aac", "ac3", "eac3", "dts", "flac", "mp3"],
            "*":    ["ac3", "eac3", "aac", "dts", "flac", "mp3"],
        }
        pref_list = preference.get(target, preference["*"])
        try:
            return pref_list.index(codec)
        except ValueError:
            return 100

    def stream_quality_key(stream: dict) -> tuple:
        rank = codec_rank_for_target(profile.audio_codec, stream.get("codec"))
        channels = stream.get("channels") or 0
        bitrate = stream.get("bitrate") or 0
        idx = stream.get("index") or 10**9
        return (rank, -channels, -bitrate, idx)

    audio_streams = inspection.get("audio_streams", []) or []

    # Allowed languages are a whitelist (NOT required)
    allowed_languages = [
        l.strip()
        for l in (profile.audio_languages or "").split(",")
        if l.strip()
    ]

    target_codec = (profile.audio_codec or "").lower() if profile.audio_codec else None
    default_language = profile.audio_def_language

    # Group streams by language
    streams_by_language: dict[str, list[dict]] = {}
    for s in audio_streams:
        lang = (s.get("language") or "und")
        streams_by_language.setdefault(lang, []).append(s)

    actions: dict[int, dict] = {}
    kept_indices: list[int] = []

    # ---------------------------
    # 1) Handle known languages (not 'und')
    # ---------------------------
    for lang, streams in streams_by_language.items():
        if lang == "und":
            continue  # handled later by 'und' logic

        # If a whitelist is defined, remove known languages not allowed.
        # If whitelist is empty, treat all known languages as allowed.
        if allowed_languages and lang not in allowed_languages:
            for s in streams:
                actions[s["index"]] = {
                    "action": "remove",
                    "target_codec": None,
                    "reason": "language_not_allowed",
                }
            continue

        # Known + allowed language group:
        # Keep only target codec streams if present; otherwise transcode best candidate.
        if target_codec:
            matching = [s for s in streams if (s.get("codec") or "").lower() == target_codec]
        else:
            matching = streams[:]  # no target codec defined -> keep as copy

        if matching and target_codec:
            for s in streams:
                if s in matching:
                    actions[s["index"]] = {
                        "action": "copy",
                        "target_codec": None,
                        "reason": "preferred_language_and_codec",
                    }
                    kept_indices.append(s["index"])
                else:
                    actions[s["index"]] = {
                        "action": "remove",
                        "target_codec": None,
                        "reason": "redundant_language_stream",
                    }
        else:
            # No matching target codec (or no target defined) -> choose best candidate
            if target_codec:
                best = sorted(streams, key=stream_quality_key)[0]
                for s in streams:
                    if s is best:
                        actions[s["index"]] = {
                            "action": "transcode",
                            "target_codec": target_codec,
                            "reason": "codec_normalization_best_candidate",
                        }
                        kept_indices.append(s["index"])
                    else:
                        actions[s["index"]] = {
                            "action": "remove",
                            "target_codec": None,
                            "reason": "redundant_language_stream",
                        }
            else:
                # No target codec -> keep first, remove rest (deterministic)
                best = sorted(streams, key=stream_quality_key)[0]
                for s in streams:
                    if s is best:
                        actions[s["index"]] = {
                            "action": "copy",
                            "target_codec": None,
                            "reason": "no_target_codec_keep_one",
                        }
                        kept_indices.append(s["index"])
                    else:
                        actions[s["index"]] = {
                            "action": "remove",
                            "target_codec": None,
                            "reason": "redundant_language_stream",
                        }

    # ---------------------------
    # 2) Decide what to do with 'und' (unknown language)
    # ---------------------------
    und_streams = streams_by_language.get("und", []) or []

    # Known languages present in the file (after overrides)
    known_langs_present = {lang for lang in streams_by_language.keys() if lang != "und"}

    # If there is a whitelist, determine whether we "miss" some allowed language among known streams.
    # Missing allowed languages means 'und' may hide an allowed language -> keep 'und'
    missing_allowed_languages = set()
    if allowed_languages:
        missing_allowed_languages = set(allowed_languages) - known_langs_present

    # Determine if we already have at least one kept audio stream from known+allowed languages
    has_valid_audio_without_und = len(kept_indices) > 0

    # Determine if any und stream has codec that is allowed or transcodable
    # (minimal definition: codec == target OR codec exists -> we can transcode later if needed)
    def und_is_codec_ok(s: dict) -> bool:
        c = (s.get("codec") or "").lower()
        if not c:
            return False
        if target_codec and c == target_codec:
            return True
        # treat any known codec as transcodable in phase 1
        return True

    und_codec_ok_exists = any(und_is_codec_ok(s) for s in und_streams)

    # - If we have a valid plan without und -> und removable UNLESS rule 4 applies (missing allowed langs + und might hide them)
    # - If we do NOT have valid audio without und -> und must be kept (never 0 audio)
    if und_streams:
        if not has_valid_audio_without_und:
            # Rule 4 (audio safety): we would end up with 0 audio, so keep und
            for s in und_streams:
                actions[s["index"]] = {
                    "action": "copy",
                    "target_codec": None,
                    "reason": "unknown_language_preserved_no_other_audio",
                }
                kept_indices.append(s["index"])

        else:
            # We have at least one valid audio without und.
            # Now check whether und might hide missing allowed language.
            if missing_allowed_languages and und_codec_ok_exists:
                # Rule 4: do NOT remove und; it may hide allowed languages not detected
                for s in und_streams:
                    actions[s["index"]] = {
                        "action": "copy",
                        "target_codec": None,
                        "reason": "unknown_language_preserved_possible_allowed_language",
                    }
                    kept_indices.append(s["index"])
            else:
                # Rule 2/3: safe to remove und
                for s in und_streams:
                    actions[s["index"]] = {
                        "action": "remove",
                        "target_codec": None,
                        "reason": "unknown_language_redundant",
                    }

    # ---------------------------
    # 3) Final safety: never end with 0 audio streams
    # ---------------------------
    if not kept_indices:
        # Keep one best available stream (prefer und if it exists, otherwise first stream)
        candidate_pool = und_streams if und_streams else audio_streams
        if candidate_pool:
            best = sorted(candidate_pool, key=stream_quality_key)[0]
            idx = best["index"]
            actions[idx] = {
                "action": "copy",
                "target_codec": None,
                "reason": "safety_keep_one_audio",
            }
            kept_indices.append(idx)

    # ---------------------------
    # 4) Assign default (only one)
    # ---------------------------
    default_assigned = False

    # Prefer kept stream in preferred default language (if defined and exists)
    if default_language:
        for s in audio_streams:
            idx = s["index"]
            if idx in kept_indices and s.get("language") == default_language and not default_assigned:
                actions[idx]["set_default"] = True
                default_assigned = True
                break

    # Fallback: first kept stream
    if not default_assigned and kept_indices:
        actions[kept_indices[0]]["set_default"] = True

    # ---------------------------
    # 5) Build final result list (one entry per original stream)
    # ---------------------------
    result = []
    for s in audio_streams:
        idx = s["index"]
        a = actions.get(idx)

        # If some stream was never assigned (should not happen), default to remove
        if not a:
            a = {"action": "remove", "target_codec": None, "reason": "unclassified"}

        result.append({
            "index": idx,
            "codec": s.get("codec"),
            "channels": s.get("channels"),
            "language": s.get("language"),
            "default": s.get("default", False),

            "action": a["action"],
            "target_codec": a.get("target_codec"),
            "set_default": a.get("set_default", False),
            "reason": a["reason"],
        })

    return result

# -------------------------------------------------
# Decide correct subtitle streams
# -------------------------------------------------

def decide_subtitle_streams(inspection: dict, profile: models.Profile) -> list:
    """
    Decide what to do with each subtitle stream.

    CONSERVATIVE mode:
    - Codec NOT allowed by profile -> REMOVE (always).
    - Known language (!= 'und') but NOT allowed -> REMOVE.
    - Language == 'und' AND codec is allowed -> KEEP (copy).
    - No subtitle transcoding.
    - It is acceptable to end up with ZERO subtitles.
    """

    subtitle_streams = inspection.get("subtitle_streams", []) or []

    # Allowed languages are a whitelist (NOT required)
    allowed_languages = [
        l.strip()
        for l in (profile.subtitle_languages or "").split(",")
        if l.strip()
    ]

    # Allowed codecs: only the target subtitle codec (if defined)
    allowed_codecs = {profile.subtitle_codec} if profile.subtitle_codec else set()

    default_language = profile.subtitle_def_language

    actions = {}
    kept_indices = []

    # ---------------------------
    # First pass: keep / remove
    # ---------------------------
    for s in subtitle_streams:
        idx = s["index"]
        lang = s.get("language") or "und"
        codec = s.get("codec")

        # 1) Codec not allowed -> REMOVE (always)
        if allowed_codecs and codec not in allowed_codecs:
            actions[idx] = {
                "action": "remove",
                "target_codec": None,
                "reason": "codec_not_allowed",
            }
            continue

        # 2) Known language but not allowed -> REMOVE
        if lang != "und" and allowed_languages and lang not in allowed_languages:
            actions[idx] = {
                "action": "remove",
                "target_codec": None,
                "reason": "language_not_allowed",
            }
            continue

        # 3) Allowed codec + (allowed language OR 'und') -> KEEP
        actions[idx] = {
            "action": "copy",
            "target_codec": None,
            "reason": (
                "subtitle_allowed"
                if lang != "und"
                else "unknown_language_preserved"
            ),
        }
        kept_indices.append(idx)

    # ---------------------------
    # Second pass: assign default subtitle (ONLY ONE)
    # ---------------------------
    default_assigned = False

    for s in subtitle_streams:
        idx = s["index"]
        if (
            idx in kept_indices
            and s.get("language") == default_language
            and s.get("forced") is True
            and not default_assigned
        ):
            actions[idx]["set_default"] = True
            default_assigned = True
            break

    # Note:
    # - We do NOT enforce having a default subtitle
    # - We do NOT force-keep subtitles if none remain

    # ---------------------------
    # Build final result list
    # ---------------------------
    result = []
    for s in subtitle_streams:
        idx = s["index"]
        a = actions.get(idx)

        # Safety fallback (should not happen)
        if not a:
            a = {"action": "remove", "target_codec": None, "reason": "unclassified"}

        result.append({
            "index": idx,
            "codec": s.get("codec"),
            "language": s.get("language"),
            "forced": s.get("forced", False),
            "default": s.get("default", False),

            "action": a["action"],
            "target_codec": None,
            "set_default": a.get("set_default", False),
            "reason": a["reason"],
        })

    return result

# -------------------------------------------------
# Job plan creation (audio + subs only)
# -------------------------------------------------

def build_job_plan(
    media: models.MediaFile,
    profile: models.Profile,
    inspection: dict,
) -> dict:
    """
    Build a job_plan based on inspection and profile.

    Phase 1:
    - Video is ALWAYS copied
    - Only audio and subtitle cleanup is planned
    """

    plan = {
        "version": 1,
        "profile": {
            "id": profile.id,
            "name": profile.name,
        },
        "input": {
            "path": media.full_path,
            "container": inspection.get("container"),
            "duration": inspection.get("duration"),
        },
        "video": {
            "action": "copy",
            "reason": "video_handling_disabled_in_phase_1",
        },
        "audio": {
            "strategy": "cleanup",
            "target_codec": profile.audio_codec,
            "default_language": profile.audio_def_language,
            "allowed_languages": (
                profile.audio_languages.split(",")
                if profile.audio_languages
                else []
            ),
            "streams": decide_audio_streams(inspection, profile),
        },
        "subtitles": {
            "strategy": "cleanup",
            "target_codec": profile.subtitle_codec,
            "default_language": profile.subtitle_def_language,
            "allowed_languages": (
                profile.subtitle_languages.split(",")
                if profile.subtitle_languages
                else []
            ),
            "streams": decide_subtitle_streams(inspection, profile),
        },
        "warnings_expected": [],
    }

    return plan

# -------------------------------------------------
# Execution
# -------------------------------------------------

def execute_job_plan(job_plan: dict, input_path: str, temp_dir: str) -> str:
    """
    Execute a real ffmpeg command based on job_plan (audio + subtitles only).
    Video is always copied.

    - Works on a temp output file inside temp_dir (never touches the original).
    - Uses absolute stream indices from ffprobe via '-map 0:<index>'.
    - Applies default dispositions based on 'set_default'.
    - Returns the temp output path.
    - Raises RuntimeError on ffmpeg failure.
    """

    os.makedirs(temp_dir, exist_ok=True)

    # Always output MKV for safety/compatibility (EAC3 + subtitles are widely supported in MKV)
    output_path = temp_output_path(input_path, temp_dir)

    # Detect input container by extension (for legacy repair flags)
    ext = os.path.splitext(input_path)[1].lower()

    cmd = ["ffmpeg", "-y"]

    # AVI files often lack proper PTS/DTS -> generate them
    if ext == ".avi":
        cmd += ["-fflags", "+genpts"]

    # Input file
    cmd += ["-i", input_path]

    # --- VIDEO: always copy ---
    cmd += ["-map", "0:v", "-c:v", "copy"]

    # Keep metadata + chapters (nice-to-have)
    cmd += ["-map_metadata", "0", "-map_chapters", "0"]

    # --- AUDIO: include only copy/transcode streams ---
    audio_out_idx = 0
    for s in job_plan.get("audio", {}).get("streams", []):
        if s.get("action") not in ("copy", "transcode"):
            continue

        # Map by absolute stream index
        cmd += ["-map", f"0:{s['index']}"]

        # Codec per output audio index
        if s["action"] == "copy":
            cmd += [f"-c:a:{audio_out_idx}", "copy"]
        else:
            # target_codec must exist for transcode entries
            cmd += [f"-c:a:{audio_out_idx}", s["target_codec"]]

        # Default disposition
        if s.get("set_default"):
            cmd += [f"-disposition:a:{audio_out_idx}", "default"]
        else:
            cmd += [f"-disposition:a:{audio_out_idx}", "0"]

        audio_out_idx += 1

    # --- SUBTITLES: include only copy streams (no subtitle transcoding in your rules) ---
    sub_out_idx = 0
    for s in job_plan.get("subtitles", {}).get("streams", []):
        if s.get("action") != "copy":
            continue

        cmd += ["-map", f"0:{s['index']}"]
        cmd += [f"-c:s:{sub_out_idx}", "copy"]

        if s.get("set_default"):
            cmd += [f"-disposition:s:{sub_out_idx}", "default"]
        else:
            cmd += [f"-disposition:s:{sub_out_idx}", "0"]

        sub_out_idx += 1

    # Output path
    cmd.append(output_path)

    # Debug: log full command
    logger.debug("ffmpeg cmd: %s", " ".join(cmd))

    # Run ffmpeg
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")

    return output_path

# -------------------------------------------------
# Verification
# -------------------------------------------------

def verify_result(temp_output_path: str, job_plan: dict) -> str:
    """
    Robust verification of ffmpeg output against job_plan.
    Focuses on existence and correctness, not strict identity.
    """

    import subprocess, json

    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        temp_output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return f"failed: ffprobe error: {result.stderr.strip()}"

    try:
        probe = json.loads(result.stdout)
    except Exception as exc:
        return f"failed: invalid ffprobe json: {exc}"

    streams = probe.get("streams", [])

    # -------- VIDEO (critical safety check) --------
    # Never accept an output without a video stream: replacing the original
    # with a video-less file would be catastrophic.
    out_video = [s for s in streams if s.get("codec_type") == "video"]
    if not out_video:
        return "failed: output has no video stream"

    # -------- DURATION (truncated/corrupt output check) --------
    # Compare against the input duration captured during inspection.
    # Tolerance: 90% (containers may round slightly, a real truncation is far below).
    fmt = probe.get("format", {}) or {}
    output_duration = _safe_float(fmt.get("duration"))
    expected_duration = _safe_float(job_plan.get("input", {}).get("duration"))

    if expected_duration and output_duration is None:
        return "failed: output duration could not be determined"

    if (
        expected_duration
        and output_duration is not None
        and output_duration < expected_duration * 0.9
    ):
        return (
            f"failed: output duration too short "
            f"({output_duration:.1f}s vs expected ~{expected_duration:.1f}s)"
        )

    out_audio = []
    out_subs = []

    for s in streams:
        stype = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}

        entry = {
            "codec": s.get("codec_name"),
            "language": tags.get("language") or "und",
            "default": bool(disp.get("default", 0)),
        }

        if stype == "audio":
            out_audio.append(entry)
        elif stype == "subtitle":
            out_subs.append(entry)

    # -------- AUDIO --------
    planned_audio = [
        s for s in job_plan["audio"]["streams"]
        if s["action"] in ("copy", "transcode")
    ]

    if len(out_audio) != len(planned_audio):
        return (
            f"failed: audio count mismatch "
            f"(expected {len(planned_audio)}, got {len(out_audio)})"
        )

    for plan in planned_audio:
        expected_codec = (
            plan["target_codec"]
            if plan["action"] == "transcode"
            else plan["codec"]
        )

        if not any(a["codec"] == expected_codec for a in out_audio):
            return f"failed: expected audio codec not found ({expected_codec})"

    if sum(a["default"] for a in out_audio) > 1:
        return "failed: more than one audio default stream"

    # -------- SUBTITLES --------
    planned_subs = [
        s for s in job_plan["subtitles"]["streams"]
        if s["action"] == "copy"
    ]

    if len(out_subs) != len(planned_subs):
        return (
            f"failed: subtitle count mismatch "
            f"(expected {len(planned_subs)}, got {len(out_subs)})"
        )

    for plan in planned_subs:
        if not any(s["codec"] == plan["codec"] for s in out_subs):
            return f"failed: expected subtitle codec not found ({plan['codec']})"

    if sum(s["default"] for s in out_subs) > 1:
        return "failed: more than one subtitle default stream"

    return "ok"

# -------------------------------------------------
# Safe replace of the original file
# -------------------------------------------------

def safe_replace_cross_fs(
    original_path: str, temp_path: str, dest_path: str | None = None
) -> None:
    """
    Safely replace original_path with temp_path when they are on different filesystems
    (e.g. temp on SSD, media on HDD).

    dest_path: optional target name (naming/rename support). Defaults to
    original_path when not given.

    Strategy:
    1. Copy temp_path to dest_path + '.thresherr.new' (on destination filesystem)
    2. fsync the copied file to ensure data is flushed to disk
    3. Atomically rename '.thresherr.new' -> dest_path (same filesystem)
    4. Remove temp_path from temp filesystem

    Guarantees:
    - If anything fails, the original file is NOT touched
    - Any partial '.thresherr.new' file is removed
    """

    if not os.path.exists(temp_path):
        raise RuntimeError("temp output file does not exist")

    if os.path.getsize(temp_path) == 0:
        raise RuntimeError("temp output file is empty")

    target = dest_path or original_path
    dst_tmp = target + ".thresherr.new"

    try:
        # 1. Copy temp file (SSD) -> destination temp file (HDD)
        with open(temp_path, "rb") as src, open(dst_tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)

            # 2. Ensure data is physically written to disk
            dst.flush()
            os.fsync(dst.fileno())

        # 3. Atomic replace on destination filesystem (HDD)
        os.replace(dst_tmp, target)

        # 4. Remove temp file from SSD
        os.remove(temp_path)

    except Exception:
        # Cleanup destination temp file if something went wrong
        try:
            if os.path.exists(dst_tmp):
                os.remove(dst_tmp)
        except Exception:
            pass

        # Re-raise so the worker marks the job as failed
        raise

# -------------------------------------------------
# Naming (*arr style, Settings -> Media Management)
# -------------------------------------------------

# Default naming template (*arr standard). Used when the setting is unset.
DEFAULT_NAMING_TEMPLATE = "{Title} ({Year}) [{Quality} {VideoCodec} {AudioLanguages}]"


def _naming_config(db) -> tuple[bool, str]:
    """(enabled, template) from the settings table."""
    enabled_row = (
        db.query(models.Setting)
        .filter(models.Setting.key == "naming_enabled")
        .first()
    )
    template_row = (
        db.query(models.Setting)
        .filter(models.Setting.key == "naming_template")
        .first()
    )
    enabled = bool(enabled_row and enabled_row.value == "1")
    template = (
        template_row.value
        if template_row and template_row.value
        else DEFAULT_NAMING_TEMPLATE
    )
    return enabled, template


def _naming_context(job: models.MediaFile, job_plan: dict, inspection: dict) -> dict:
    """Build the token values for naming from a job's plan and inspection."""
    # Clean title: release noise (year/quality/source/codecs) is removed so
    # the {Title}/{Year} tokens never duplicate info from the original name.
    raw_title = os.path.splitext(os.path.basename(job.full_path))[0]
    title = naming.clean_title(job.full_path)
    year = naming.extract_year(raw_title)

    kept_audio = [
        s
        for s in job_plan.get("audio", {}).get("streams", [])
        if s.get("action") in ("copy", "transcode")
    ]
    kept_subs = [
        s
        for s in job_plan.get("subtitles", {}).get("streams", [])
        if s.get("action") in ("copy", "transcode")
    ]

    audio_codecs: list[str] = []
    audio_langs: list[str] = []
    for s in kept_audio:
        codec = s.get("target_codec") or s.get("codec")
        if codec and codec not in audio_codecs:
            audio_codecs.append(codec)
        lang = naming.short_language(s.get("language"))
        if lang and lang not in audio_langs:
            audio_langs.append(lang)

    sub_langs: list[str] = []
    for s in kept_subs:
        lang = naming.short_language(s.get("language"))
        if lang and lang not in sub_langs:
            sub_langs.append(lang)

    video = inspection.get("video", {}) or {}
    height = video.get("height")

    return {
        "title": title,
        "year": year,
        "quality": naming.quality_from_dimensions(
            video.get("width"), height
        )
        or "",
        "video_codec": video.get("codec") or "",
        "audio_codecs": "+".join(audio_codecs),
        "audio_languages": "-".join(audio_langs),
        "subtitle_languages": "-".join(sub_langs),
        # Phase 1: the worker ALWAYS outputs mkv (the job plan has no
        # container key) — explicit here so the naming never changes by
        # accident if the plan grows a container field.
        "container": "mkv",
    }


def build_output_name_from_job(
    job: models.MediaFile, job_plan: dict, inspection: dict, template: str
) -> str | None:
    """Full output file name for a job, or None to keep the original name."""
    ctx = _naming_context(job, job_plan, inspection)
    return naming.build_output_name(template=template, **ctx)


# -------------------------------------------------
# Recycle bin (*arr style, Settings -> Media Management)
# -------------------------------------------------
# Config + cleanup logic moved to tasks.py (System -> Tasks also uses them).
# move_to_recycle_bin stays here: it is part of the job replace flow.

def move_to_recycle_bin(source_path: str, recycle_dir: str) -> str:
    """
    Move the original file into the recycle bin before replacement.

    The mtime is reset to 'now' so retention counts from entry into the
    bin (a 2010 file parked today must NOT be purged tomorrow).
    """
    os.makedirs(recycle_dir, exist_ok=True)
    dest = naming.unique_dest_path(recycle_dir, os.path.basename(source_path))
    shutil.move(source_path, dest)  # works cross-filesystem (copy + remove)
    now = time.time()
    os.utime(dest, (now, now))
    return dest


# -------------------------------------------------
# Automatic backups (*arr style, System -> Backups)
# -------------------------------------------------

def maybe_automatic_backup(db) -> str | None:
    """
    Create a backup when the configured interval (days) has elapsed since
    the last one. Returns the created backup file name, or None when not
    due or disabled (interval 0).
    """
    interval = settings.get_int(db, "backup_interval_days", 7)
    if interval <= 0:
        return None

    existing = backups.list_backups(backups.BACKUP_DIR)
    if existing and (time.time() - existing[0]["mtime"]) < interval * 86400:
        return None  # not due yet

    path = backups.create_backup(DB_PATH, backups.BACKUP_DIR)
    retention = settings.get_int(db, "backup_retention", 7)
    removed = backups.enforce_retention(backups.BACKUP_DIR, retention)
    if removed:
        logger.info("Backup retention removed %s old backup(s)", removed)
    return os.path.basename(path)


# -------------------------------------------------
# Main worker loop
# -------------------------------------------------

def run_worker():
    setup_logging()
    logger.info("Worker starting")

    # First heartbeat + worker start time (kept on restarts)
    _boot_db = SessionLocal()
    try:
        if settings.get_setting(_boot_db, "worker_started_at") is None:
            _set_worker_setting(_boot_db, "worker_started_at", _utcnow().isoformat())
        write_heartbeat(_boot_db, force=True)
    finally:
        _boot_db.close()

    # Periodic recycle bin cleanup (~every hour with 5s sleep)
    RECYCLE_CLEANUP_EVERY = 720
    cycle_count = 0

    while True:
        db = SessionLocal()
        job = None
        cycle_count += 1
        try:
            # Liveness heartbeat (throttled)
            write_heartbeat(db)

            # Recover jobs stuck in 'processing' (crashed/killed worker)
            requeue_stale_processing(db)

            job = claim_next_job(db)

            if not job:
                time.sleep(WORKER_SLEEP_SECONDS)
                continue

            logger.info("Processing media_file id=%s (%s)", job.id, job.file_name)

            profile = job.library.profile

            inspection = inspect_file(job)
            
            # Temporally
            logger.debug(
                "inspect: container=%s audio=%s subs=%s",
                inspection.get("container"),
                len(inspection.get("audio_streams", [])),
                len(inspection.get("subtitle_streams", [])),
            )
            #############

            job_plan = build_job_plan(job, profile, inspection)
            job.job_plan = json.dumps(job_plan, indent=2)
            db.commit()

            
            temp_output = execute_job_plan(job_plan, input_path=job.full_path, temp_dir=job.library.temp_path,)
            
            # Temporally
            logger.debug(
                "audio plan: %s",
                [(s["index"], s["action"], s["language"], s["codec"], s.get("target_codec")) for s in job_plan["audio"]["streams"]],
            )

            logger.debug(
                "subtitle plan: %s",
                [(s["index"], s["action"], s["language"], s["codec"], "forced" if s.get("forced") else "full", "DEFAULT" if s.get("set_default") else "") for s in job_plan["subtitles"]["streams"]],
            )
            #############

            verification = verify_result(temp_output, job_plan)
            
            logger.info("verification: %s", verification)
            
            job.verification_result = verification

            if verification == "ok":
                # Naming (Settings -> Media Management): rename on the fly
                final_path = job.full_path
                naming_enabled, naming_template = _naming_config(db)
                if naming_enabled:
                    new_name = build_output_name_from_job(
                        job, job_plan, inspection, naming_template
                    )
                    if new_name and new_name != os.path.basename(job.full_path):
                        final_path = naming.unique_dest_path(
                            os.path.dirname(job.full_path), new_name
                        )
                        logger.info(
                            "Renaming output: %s -> %s",
                            os.path.basename(job.full_path),
                            os.path.basename(final_path),
                        )

                # Recycle bin: park the ORIGINAL file before it gets replaced
                recycle_enabled, recycle_path, _ = tasks.recycle_config(db)
                if recycle_enabled and recycle_path:
                    try:
                        parked = move_to_recycle_bin(job.full_path, recycle_path)
                        logger.info(
                            "Original moved to recycle bin: %s",
                            os.path.basename(parked),
                        )
                    except Exception as exc:
                        # Never lose the original: fail the job instead
                        raise RuntimeError(
                            f"could not move original to recycle bin: {exc}"
                        ) from exc

                safe_replace_cross_fs(job.full_path, temp_output, dest_path=final_path)

                # Re-scan final file for UI metadata
                final_meta = get_video_metadata(final_path)
                job.video_codec = final_meta.get("video_codec")
                job.resolution = final_meta.get("resolution")
                job.audio_codec = final_meta.get("audio_codec")
                job.audio_languages = final_meta.get("audio_languages")
                job.subtitle_codec = final_meta.get("subtitle_codec")
                job.subtitle_languages = final_meta.get("subtitle_languages")

                job.full_path = final_path
                job.file_name = os.path.basename(final_path)
                job.status = "completed"
                job.size_final = os.path.getsize(final_path)
            else:
                job.status = "failed"
                job.last_error = verification
                # Do not leave the non-compliant temp output behind
                remove_temp_output(job)

            job.finished_at = _utcnow()
            db.commit()

            logger.info("Finished media_file id=%s status=%s", job.id, job.status)

        except Exception as exc:
            db.rollback()
            # If a job was already claimed, mark it as failed instead of
            # leaving it stuck in 'processing' forever.
            if job is not None:
                try:
                    job.status = "failed"
                    job.last_error = f"worker exception: {exc}"
                    job.finished_at = _utcnow()
                    db.commit()
                    remove_temp_output(job)
                    logger.error("Marked job id=%s as failed: %s", job.id, exc)
                except Exception as inner:
                    db.rollback()
                    logger.error("Could not mark job id=%s as failed: %s", job.id, inner)
            logger.exception("Unhandled worker error: %s", exc)

        finally:
            db.close()

            # Housekeeping (cheap, once per hour): recycle cleanup,
            # automatic backups and the scheduled scan (System -> Tasks)
            if cycle_count % RECYCLE_CLEANUP_EVERY == 0:
                _cleanup_db = SessionLocal()
                try:
                    try:
                        out = tasks.run_recycle_cleanup(_cleanup_db)
                        if out.get("removed"):
                            logger.info(
                                "Recycle bin cleanup: removed %s file(s)",
                                out["removed"],
                            )
                    except Exception as exc:
                        logger.error("Recycle bin cleanup failed: %s", exc)

                    try:
                        created = maybe_automatic_backup(_cleanup_db)
                        if created:
                            logger.info(
                                "Automatic backup created: %s", created
                            )
                    except Exception as exc:
                        logger.error("Automatic backup failed: %s", exc)

                    try:
                        result = tasks.maybe_scheduled_scan(_cleanup_db)
                        if result:
                            logger.info("Scheduled scan: %s", result)
                    except Exception as exc:
                        logger.error("Scheduled scan failed: %s", exc)
                finally:
                    _cleanup_db.close()


if __name__ == "__main__":
    run_worker()