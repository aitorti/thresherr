import os
import time
import json
import subprocess
import shutil
import models

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from scanner import get_video_metadata
from scanner import infer_stream_language
from database import SessionLocal, engine

# Ensure DB schema exists
models.Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# Worker configuration
# -------------------------------------------------

WORKER_SLEEP_SECONDS = 5

# Jobs stuck in 'processing' for longer than this are considered stale
# (crashed/killed worker) and are automatically re-queued.
STALE_PROCESSING_TIMEOUT_MINUTES = 60

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
            print(
                f"[worker] removed temp output: {temp_output}",
                flush=True,
            )
    except Exception as exc:
        print(f"[worker] could not remove temp output: {exc}", flush=True)


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
    job.started_at = datetime.utcnow()
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

    cutoff = datetime.utcnow() - timedelta(minutes=STALE_PROCESSING_TIMEOUT_MINUTES)

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
        print(
            f"[worker] re-queued stale job id={job.id} ({job.file_name})",
            flush=True,
        )

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

    # Debug: print full command
    print("[worker] ffmpeg cmd:", " ".join(cmd), flush=True)

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

def safe_replace_cross_fs(original_path: str, temp_path: str) -> None:
    """
    Safely replace original_path with temp_path when they are on different filesystems
    (e.g. temp on SSD, media on HDD).

    Strategy:
    1. Copy temp_path to original_path + '.thresherr.new' (on destination filesystem)
    2. fsync the copied file to ensure data is flushed to disk
    3. Atomically rename '.thresherr.new' -> original_path (same filesystem)
    4. Remove temp_path from temp filesystem

    Guarantees:
    - If anything fails, the original file is NOT touched
    - Any partial '.thresherr.new' file is removed
    """

    if not os.path.exists(temp_path):
        raise RuntimeError("temp output file does not exist")

    if os.path.getsize(temp_path) == 0:
        raise RuntimeError("temp output file is empty")

    dst_tmp = original_path + ".thresherr.new"

    try:
        # 1. Copy temp file (SSD) -> destination temp file (HDD)
        with open(temp_path, "rb") as src, open(dst_tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)

            # 2. Ensure data is physically written to disk
            dst.flush()
            os.fsync(dst.fileno())

        # 3. Atomic replace on destination filesystem (HDD)
        os.replace(dst_tmp, original_path)

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
# Main worker loop
# -------------------------------------------------

def run_worker():
    print("[worker] starting", flush=True)

    while True:
        db = SessionLocal()
        job = None
        try:
            # Recover jobs stuck in 'processing' (crashed/killed worker)
            requeue_stale_processing(db)

            job = claim_next_job(db)

            if not job:
                time.sleep(WORKER_SLEEP_SECONDS)
                continue

            print(f"[worker] processing media_file id={job.id}", flush=True)

            profile = job.library.profile

            inspection = inspect_file(job)
            
            # Temporally
            print(f"[worker] inspect: container={inspection.get('container')} "f"audio={len(inspection.get('audio_streams', []))} "f"subs={len(inspection.get('subtitle_streams', []))}", flush=True)
            #############

            job_plan = build_job_plan(job, profile, inspection)
            job.job_plan = json.dumps(job_plan, indent=2)
            db.commit()

            
            temp_output = execute_job_plan(job_plan, input_path=job.full_path, temp_dir=job.library.temp_path,)
            
            # Temporally
            print("[worker] audio plan:",[(s["index"], s["action"], s["language"], s["codec"], s.get("target_codec")) for s in job_plan["audio"]["streams"]], flush=True)
            
            print(
                "[worker] subtitle plan:",[(s["index"], s["action"], s["language"], s["codec"],"forced" if s.get("forced") else "full", "DEFAULT" if s.get("set_default") else "") for s in job_plan["subtitles"]["streams"]], flush=True,)
            #############

            verification = verify_result(temp_output, job_plan)
            
            print(f"[worker] verification: {verification}", flush=True)
            
            job.verification_result = verification

            if verification == "ok":
                
                safe_replace_cross_fs(job.full_path, temp_output)
            
                # Re-scan final file for UI metadata
                final_meta = get_video_metadata(job.full_path)
                job.video_codec = final_meta.get("video_codec")
                job.resolution = final_meta.get("resolution")
                job.audio_codec = final_meta.get("audio_codec")
                job.audio_languages = final_meta.get("audio_languages")
                job.subtitle_codec = final_meta.get("subtitle_codec")
                job.subtitle_languages = final_meta.get("subtitle_languages")

                job.status = "completed"
                job.size_final = os.path.getsize(job.full_path)
            else:
                job.status = "failed"
                job.last_error = verification
                # Do not leave the non-compliant temp output behind
                remove_temp_output(job)

            job.finished_at = datetime.utcnow()
            db.commit()

            print(f"[worker] finished media_file id={job.id} status={job.status}", flush=True)

        except Exception as exc:
            db.rollback()
            # If a job was already claimed, mark it as failed instead of
            # leaving it stuck in 'processing' forever.
            if job is not None:
                try:
                    job.status = "failed"
                    job.last_error = f"worker exception: {exc}"
                    job.finished_at = datetime.utcnow()
                    db.commit()
                    remove_temp_output(job)
                    print(
                        f"[worker] marked job id={job.id} as failed: {exc}",
                        flush=True,
                    )
                except Exception as inner:
                    db.rollback()
                    print(
                        f"[worker] could not mark job id={job.id} as failed: {inner}",
                        flush=True,
                    )
            print(f"[worker] ERROR: {exc}", flush=True)

        finally:
            db.close()


if __name__ == "__main__":
    run_worker()