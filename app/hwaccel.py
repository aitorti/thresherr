"""
Hardware acceleration resolution + probe (worker side).

THRESHERR_ACCEL (env var, injected by the docker-compose `extends` block —
see hwaccel.transcoding.yml) selects the transcode backend at install time,
Immich-style:

    cpu | nvenc | vaapi | qsv

The worker probes the backend at startup with a REAL one-frame ffmpeg
encode (encoder listings are static — only an actual init proves the
device works) and persists the outcome as worker settings so Health can
warn when a requested backend fell back to CPU.
"""

import logging
import os
import subprocess

import compat

logger = logging.getLogger("Thresherr.Worker")

ENV_VAR = "THRESHERR_ACCEL"
DEFAULT_ACCEL = "cpu"

# Global init args that MUST appear before `-i` for these families.
GLOBAL_PRE_INPUT = {
    "qsv": ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"],
    "vaapi": ["-vaapi_device", "/dev/dri/renderD128"],
}

# Probe recipes: extra pre-input args + a hwupload filter for the family.
# NOTE: keep the source ABOVE NVENC's minimum frame dimension (>= 65px);
# 64x64 fails with "Frame Dimension less than the minimum supported value".
_SRC = "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=1"
_PROBE = {
    "nvenc": ([*_SRC], []),
    "qsv": (["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw", *_SRC],
            ["format=nv12", "hwupload=extra_hw_frames=64"]),
    "vaapi": (["-vaapi_device", "/dev/dri/renderD128", *_SRC],
              ["format=nv12", "hwupload"]),
}


def requested_accel() -> str:
    """Accel from the environment, validated; unknown values -> cpu."""
    accel = (os.environ.get(ENV_VAR) or "").strip().lower()
    if not accel or accel == "auto":
        accel = DEFAULT_ACCEL
    if not compat.is_valid_accel(accel):
        logger.warning(
            "%s=%r is not a valid backend (cpu|nvenc|vaapi|qsv) -> using cpu",
            ENV_VAR, accel,
        )
        return DEFAULT_ACCEL
    return accel


def probe(accel: str) -> tuple[bool, str]:
    """
    Prove the backend with a real 1-frame encode of its H.264 encoder.

    Returns (ok, detail). `ok=False` means "requested backend unusable —
    the caller must fall back to CPU". cpu always probes ok.
    """
    if accel == "cpu":
        return True, "cpu software encoding"

    encoder = compat.accel_encoder(accel, "h264")
    pre, filters = _PROBE.get(accel, ([], []))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    cmd += list(pre)
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-frames:v", "1", "-c:v", encoder, "-f", "null", "-"]

    logger.debug("hwaccel probe: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return False, "ffmpeg not found in worker image"
    except subprocess.TimeoutExpired:
        return False, f"probe timed out for {encoder}"
    except Exception as exc:  # e.g. EACCES on /dev/dri, driver errors
        return False, f"probe failed: {exc}"

    if result.returncode == 0:
        return True, f"{encoder} encode OK"
    # Keep the probe failure short & useful (last non-empty line).
    detail = (result.stderr or "").strip().splitlines()
    detail = detail[-1] if detail else f"{encoder} init failed"
    return False, f"{encoder}: {detail[:200]}"


def pre_input_args(encoder: str) -> list[str]:
    """Global args needed before -i for a given encoder ('' for cpu/nvenc)."""
    return list(GLOBAL_PRE_INPUT.get(compat.encoder_family(encoder), []))
