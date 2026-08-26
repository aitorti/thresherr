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

from worker import build_job_plan


def _allowed_languages(raw: str | None) -> list[str]:
    return [l.strip() for l in (raw or "").split(",") if l.strip()]


def _lang_list(raw: str | None) -> list[str]:
    return [l.strip() for l in (raw or "").split(",") if l.strip()]


def _is_mkv(media) -> bool:
    """The worker always outputs .mkv (phase 1), so only .mkv is 'done'."""
    return os.path.splitext(media.full_path or "")[1].lower() == ".mkv"


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
    sub_codecs = {profile.subtitle_codec} if profile.subtitle_codec else set()
    if sub_codecs and (media.subtitle_codec or "").lower() not in sub_codecs:
        return False

    sub_allowed = _allowed_languages(profile.subtitle_languages)
    if sub_allowed:
        sub_langs = _lang_list(media.subtitle_languages)
        if not sub_langs:
            return False
        for lang in sub_langs:
            if lang == "und":
                return False
            if lang not in sub_allowed:
                return False

    # --- Container (phase 1: worker always outputs mkv) ---
    if not _is_mkv(media):
        return False

    # Video is always copied in phase 1 -> no constraint
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

    if not _is_mkv(media):
        return False

    return True
