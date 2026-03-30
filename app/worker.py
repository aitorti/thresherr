import time
import json
from datetime import datetime

from sqlalchemy.orm import Session

from database import SessionLocal
import models


# -------------------------------------------------
# Worker configuration
# -------------------------------------------------

WORKER_SLEEP_SECONDS = 5


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
        .with_for_update(skip_locked=True)
        .first()
    )

    if not job:
        return None

    job.status = "processing"
    job.started_at = datetime.utcnow()
    db.commit()

    return job


# -------------------------------------------------
# Inspection (placeholder)
# -------------------------------------------------

def inspect_file(media: models.MediaFile) -> dict:
    """
    Inspect the media file.

    NOTE:
    - This is a placeholder.
    - Real ffprobe-based inspection will be added later.
    """

    return {
        "container": "mkv",
        "video": {
            "codec": media.video_codec,
            "resolution": media.resolution,
            "bitrate": None,
        },
        "audio_streams": [],
        "subtitle_streams": [],
    }


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
            "target_codec": profile.audio_codec,
            "default_language": profile.audio_def_language,
            "allowed_languages": (
                profile.audio_languages.split(",")
                if profile.audio_languages
                else []
            ),
            "streams": [],
        },
        "subtitles": {
            "target_codec": profile.subtitle_codec,
            "default_language": profile.subtitle_def_language,
            "allowed_languages": (
                profile.subtitle_languages.split(",")
                if profile.subtitle_languages
                else []
            ),
            "streams": [],
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
    print("[worker] starting")

    while True:
        db = SessionLocal()
        try:
            job = claim_next_job(db)

            if not job:
                time.sleep(WORKER_SLEEP_SECONDS)
                continue

            print(f"[worker] processing media_file id={job.id}")

            profile = job.library.profile

            inspection = inspect_file(job)

            job_plan = build_job_plan(job, profile, inspection)
            job.job_plan = json.dumps(job_plan, indent=2)
            db.commit()

            execute_job_plan(job_plan)

            verification = verify_result(job, profile)
            job.verification_result = verification

            if verification == "ok":
                job.status = "completed"
            else:
                job.status = "failed"
                job.last_error = verification

            job.finished_at = datetime.utcnow()
            db.commit()

            print(f"[worker] finished media_file id={job.id} status={job.status}")

        except Exception as exc:
            db.rollback()
            print(f"[worker] ERROR: {exc}")

        finally:
            db.close()


if __name__ == "__main__":
    run_worker()
``
