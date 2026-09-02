"""
Profile compliance (*arr philosophy: a file that already matches the
profile is never touched).

Two evaluation levels:

- compliance_from_summary(media, profile): cheap check using the scan
  summary stored in the DB (codecs + languages + extension). Used by the
  dashboard (Perfil OK badge from the first scan) and by batch actions.
  Conservative: files with stream_overrides or missing data are NOT
  marked compliant (the individual enqueue re-checks with a fresh probe).

- compliance_from_inspection(media, inspection, profile): exact check
  using a fresh ffprobe inspection and the real job plan. Used by the
  individual enqueue/rescan. A file is compliant when the plan would only
  copy streams (no removals/transcodes), no default disposition would
  change and the container is already the worker's output (mkv).

Pure-ish module: no FastAPI. Imports the decision functions from worker
(no cycle: worker does not import compliance).
"""

import os
import re

import compat
from worker import build_job_plan


def _allowed_languages(raw: str | None) -> list[str]:
    return [l.strip() for l in (raw or "").split(",") if l.strip()]


def _lang_list(raw: str | None) -> list[str]:
    return [l.strip() for l in (raw or "").split(",") if l.strip()]


def _matches_container(media, profile) -> bool:
    """The file extension must be the profile's output container."""
    container = (profile.container or "mkv").lower()
    ext = compat.CONTAINER_EXT.get(container, ".mkv")
    return os.path.splitext(media.full_path or "")[1].lower() == ext


def _resolution_tier(label: str | None) -> int | None:
    """'1080p' -> 1080; None when there is no number."""
    if not label:
        return None
    match = re.search(r"(\d+)", label)
    return int(match.group(1)) if match else None


# -------------------------------------------------
# Summary-based check (scan data, no ffprobe)
# -------------------------------------------------

def compliance_from_summary(media, profile) -> bool:
    """
    Cheap compliance check from the scan summary.

    Returns False (not compliant / unknown) for anything uncertain:
    stream overrides present, missing language data, 'und' languages
    (they need a human decision anyway) — the individual enqueue does the
    exact check with a fresh probe.
    """
    if media.stream_overrides:
        return False

    # --- Audio ---
    target_audio = (profile.audio_codec or "").lower()
    if target_audio and (media.audio_codec or "").lower() != target_audio:
        return False

    audio_allowed = _allowed_languages(profile.audio_languages)
    if audio_allowed:
        audio_langs = _lang_list(media.audio_languages)
        if not audio_langs:
            return False
        for lang in audio_langs:
            if lang == "und":
                return False
            if lang not in audio_allowed:
                return False

    # --- Subtitles ---
    # The profile whitelist is "allowed", not "required": a file WITHOUT
    # subtitles is fully compliant (nothing to clean). If subtitles exist
    # they must be in allowed languages/codecs — otherwise the plan would
    # remove them and the file is "not compliant until processed".
    sub_langs = _lang_list(media.subtitle_languages)
    if sub_langs:
        target_sub = (profile.subtitle_codec or "").lower()
        if target_sub == "none":
            # Profile demands NO subtitles in the output
            return False
        allowed_sub = {
            compat.SUBTITLE_EQUIV.get(target_sub, target_sub)
        } if target_sub else set()
        media_sub_codecs = [
            compat.SUBTITLE_EQUIV.get(c.strip().lower(), c.strip().lower())
            for c in (media.subtitle_codec or "").split(",")
            if c.strip()
        ]
        if allowed_sub and media_sub_codecs and any(
            c not in allowed_sub for c in media_sub_codecs
        ):
            return False

        sub_allowed = _allowed_languages(profile.subtitle_languages)
        if sub_allowed:
            for lang in sub_langs:
                if lang == "und":
                    return False
                if lang not in sub_allowed:
                    return False

    # --- Video (phase 2: codec / resolution cap / bitrate cap) ---
    target_video = compat.VIDEO_FFPROBE.get((profile.video_codec or "").lower())
    if target_video and (media.video_codec or "").lower() != target_video:
        return False

    max_res = profile.video_max_res or 0
    if max_res:
        res_num = _resolution_tier(media.resolution)
        if res_num is None:
            # Unknown resolution: conservative (the individual enqueue
            # re-checks with a fresh probe).
            return False
        if res_num > max_res:
            return False

    max_kbps = profile.video_max_bitrate or 0
    # Bitrate is only enforced when known (scan summary may lack it for
    # legacy rows until the next scan; the worker enforces it regardless).
    if max_kbps and media.video_bitrate and media.video_bitrate > max_kbps * 1000:
        return False

    # --- Container (the file must already be the profile output) ---
    if not _matches_container(media, profile):
        return False

    return True


# -------------------------------------------------
# Inspection-based check (fresh ffprobe + real plan)
# -------------------------------------------------

def compliance_from_inspection(media, inspection: dict, profile) -> bool:
    """
    Exact check: build the real job plan and verify it changes nothing.

    - every stream action must be 'copy' (no remove/transcode)
    - a 'set_default' change means the file would be rewritten
    - the container must already be the worker output (mkv)
    """
    plan = build_job_plan(media, profile, inspection)

    # The video must be copied as-is (no transcode planned)
    video = plan.get("video", {}) or {}
    if video.get("action") != "copy":
        return False

    for stream in plan.get("audio", {}).get("streams", []):
        if stream.get("action") != "copy":
            return False
        if stream.get("set_default") and not stream.get("default"):
            return False

    for stream in plan.get("subtitles", {}).get("streams", []):
        if stream.get("action") != "copy":
            return False
        if stream.get("set_default") and not stream.get("default"):
            return False

    if not _matches_container(media, profile):
        return False

    return True
