import time
import json
import subprocess
import re
from datetime import datetime

from sqlalchemy.orm import Session

from database import SessionLocal, engine
import models


# Ensure DB schema exists
models.Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# Worker configuration
# -------------------------------------------------

WORKER_SLEEP_SECONDS = 5

# -------------------------------------------------
# Stream ad cleaning
# -------------------------------------------------

def _clean_stream_title(title: str) -> str:
    if not title:
        return ""
    spam_patterns = [
        r"\[.*?\]",
        r"\(.*?\)",
        r"www\..*?\.[a-z]+",
        r"@[\w_]+",
        r"\bby\s+\w+\b",
    ]
    clean = title
    for pattern in spam_patterns:
        clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)
    return clean.strip().lower()

# -------------------------------------------------
# Distinguishing between Castilian Spanish and Latin American Spanish
# -------------------------------------------------

def _refine_spanish_language(tags: dict) -> str:
    """
    Igual que en scanner: distinguir 'spa' (castellano) de 'latam' (latino).
    Usa tags.language y tags.title.
    """
    lang = (tags.get("language") or "").lower()
    title = _clean_stream_title(tags.get("title", ""))

    latam_keywords = [
        "lat",
        "latin",
        "latino",
        "latam",
        "latinoamericano",
        "américa",
        "americano",
    ]

    if lang in {"spa", "es", "esp"}:
        if any(k in title for k in latam_keywords):
            return "latam"
        return "spa"

    if any(k == lang for k in latam_keywords):
        return "latam"

    return lang if lang else "und"

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
        lang = _refine_spanish_language(tags)

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
        return Non

# -------------------------------------------------
# Decide correct audio streams
# -------------------------------------------------


def decide_audio_streams(inspection: dict, profile: models.Profile) -> list:
    """
    Decide what to do with each audio stream based on inspection + profile.

    Refined rules:
    - All streams are evaluated.
    - Non-allowed languages are removed.
    - For each allowed language:
        * If any stream already uses the target codec -> keep only those, remove the rest.
        * Otherwise -> transcode ONE best candidate, remove the rest.
    - Only ONE stream must be marked as default (prefer profile.audio_def_language).
    """

    def codec_rank_for_target(target: str, codec: str | None) -> int:
        """
        Lower rank = better candidate to transcode INTO target codec.

        Key idea:
        - For EAC3, prefer AC3 over DTS (Dolby->Dolby usually degrades less than DTS->Dolby).
        - Keep this mapping small and opinionated; extend later if needed.
        """
        if not codec:
            return 999

        target = (target or "").lower()
        codec = codec.lower()

        preference = {
            # Best sources to reach EAC3:
            "eac3": ["eac3", "ac3", "aac", "dts", "flac", "mp3"],
            # Best sources to reach AC3:
            "ac3":  ["ac3", "eac3", "aac", "dts", "flac", "mp3"],
            # Best sources to reach AAC:
            "aac":  ["aac", "ac3", "eac3", "dts", "flac", "mp3"],
            # Default fallback (if target unknown):
            "*":    ["ac3", "eac3", "aac", "dts", "flac", "mp3"],
        }

        pref_list = preference.get(target, preference["*"])
        try:
            return pref_list.index(codec)
        except ValueError:
            return 100  # unknown codecs go last

    def stream_quality_key(stream: dict) -> tuple:
        """
        Sorting key to choose best transcode candidate.
        Priority:
        1) codec preference rank (lower is better)
        2) channels (higher is better)
        3) bitrate (higher is better, if present)
        4) index (lower is deterministic fallback)
        """
        rank = codec_rank_for_target(profile.audio_codec, stream.get("codec"))
        channels = stream.get("channels") or 0
        bitrate = stream.get("bitrate") or 0
        idx = stream.get("index") or 10**9
        return (rank, -channels, -bitrate, idx)

    audio_streams = inspection.get("audio_streams", [])

    allowed_languages = [
        l.strip()
        for l in (profile.audio_languages or "").split(",")
        if l.strip()
    ]
    target_codec = profile.audio_codec
    default_language = profile.audio_def_language

    # Group streams by language
    streams_by_language = {}
    for s in audio_streams:
        streams_by_language.setdefault(s.get("language"), []).append(s)

    actions = {}
    kept_indices = []

    # Decide actions per language group
    for lang, streams in streams_by_language.items():

        # Language not allowed -> remove all
        if lang not in allowed_languages:
            for s in streams:
                actions[s["index"]] = {
                    "action": "remove",
                    "target_codec": None,
                    "reason": "language_not_allowed",
                }
            continue

        # If any stream already has target codec -> keep only those
        matching = [s for s in streams if s.get("codec") == target_codec]

        if matching:
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
            # No stream with target codec -> transcode ONE best candidate
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

    # Assign default (only one)
    default_assigned = False

    # First: pick kept stream with preferred default language
    for s in audio_streams:
        idx = s["index"]
        if idx in kept_indices and s.get("language") == default_language and not default_assigned:
            actions[idx]["set_default"] = True
            default_assigned = True

    # Fallback: first kept stream
    if not default_assigned and kept_indices:
        actions[kept_indices[0]]["set_default"] = True

    # Build final list (one entry per original stream)
    result = []
    for s in audio_streams:
        idx = s["index"]
        a = actions[idx]
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

    Rules:
    - Subtitle is REMOVED if:
        * language is not allowed, OR
        * codec is not allowed
    - No subtitle transcoding is attempted.
    - It is acceptable to end up with zero subtitles.
    - From remaining subtitles:
        * If a FORCED subtitle exists in default language -> set it as default
        * Otherwise -> no default subtitle is set
    """

    subtitle_streams = inspection.get("subtitle_streams", [])

    allowed_languages = [
        l.strip()
        for l in (profile.subtitle_languages or "").split(",")
        if l.strip()
    ]

    # Target codec defines the allowed subtitle codecs
    # Example: profile.subtitle_codec == "subrip"
    allowed_codecs = {profile.subtitle_codec} if profile.subtitle_codec else set()

    default_language = profile.subtitle_def_language

    actions = {}
    kept_indices = []

    # First pass: decide keep/remove
    for s in subtitle_streams:
        idx = s["index"]
        lang = s.get("language")
        codec = s.get("codec")

        # Remove if language not allowed
        if lang not in allowed_languages:
            actions[idx] = {
                "action": "remove",
                "target_codec": None,
                "reason": "language_not_allowed",
            }
            continue

        # Remove if codec not allowed
        if codec not in allowed_codecs:
            actions[idx] = {
                "action": "remove",
                "target_codec": None,
                "reason": "codec_not_allowed",
            }
            continue

        # Otherwise keep (copy)
        actions[idx] = {
            "action": "copy",
            "target_codec": None,
            "reason": "subtitle_allowed",
        }
        kept_indices.append(idx)

    # Second pass: assign default subtitle (ONLY ONE)
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

    # Build final result list
    result = []
    for s in subtitle_streams:
        idx = s["index"]
        a = actions[idx]

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
# Execution (NO-OP for now)
# -------------------------------------------------

def execute_job_plan(job_plan: dict) -> None:
    """
    Execute the job_plan.

    Phase 1:
    - No real execution yet
    - Placeholder for future audio/subtitle processing
    """
    return


# -------------------------------------------------
# Verification (placeholder)
# -------------------------------------------------

def verify_result(media: models.MediaFile, profile: models.Profile) -> str:
    """
    Verify the result.

    Phase 1:
    - Always return 'ok'
    """
    return "ok"


# -------------------------------------------------
# Main worker loop
# -------------------------------------------------

def run_worker():
    print("[worker] starting", flush=True)

    while True:
        db = SessionLocal()
        try:
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

            execute_job_plan(job_plan)
            
            # Temporally
            print("[worker] audio plan:",[(s["index"], s["action"], s["language"], s["codec"], s.get("target_codec")) for s in job_plan["audio"]["streams"]], flush=True)
            
            print(
                "[worker] subtitle plan:",
                [
                    (
                        s["index"],
                        s["action"],
                        s["language"],
                        s["codec"],
                        "forced" if s.get("forced") else "full",
                        "DEFAULT" if s.get("set_default") else ""
                    )
                    for s in job_plan["subtitles"]["streams"]
                ],
                flush=True,
            )
            #############

            verification = verify_result(job, profile)
            job.verification_result = verification

            if verification == "ok":
                job.status = "completed"
                # Fase NO-OP: no transformamos el archivo, así que tamaño final = original
                job.size_final = job.size_original
            else:
                job.status = "failed"
                job.last_error = verification

            job.finished_at = datetime.utcnow()
            db.commit()

            print(f"[worker] finished media_file id={job.id} status={job.status}", flush=True)

        except Exception as exc:
            db.rollback()
            print(f"[worker] ERROR: {exc}", flush=True)

        finally:
            db.close()


if __name__ == "__main__":
    run_worker()