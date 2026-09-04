"""
Media container/codec compatibility matrix (phase 2 video).

Single source of truth used by:
  - the profile form (which options are selectable per container),
  - profile validation on save,
  - the worker (container-honouring muxing, subtitle conversions),
  - the compliance checks.

Profile codec keys (as stored in DB / sent by the UI):
  video:  h264 | h265 | av1 | vp9
  audio:  aac | ac3 | eac3 | dts | flac | mp3 | opus | vorbis
  sub:    subrip | vtt | ass | pgs
"""

import re

CONTAINERS = ("mkv", "mp4", "webm", "avi")
CONTAINER_EXT = {
    "mkv": ".mkv",
    "mp4": ".mp4",
    "webm": ".webm",
    "avi": ".avi",
}

# Human labels for the UI matrix (English, like the rest of the UI chrome;
# the *arr family keeps technical option labels untranslated too).
CONTAINER_HINTS = {
    "mkv": "Any codec; text and image subtitles.",
    "mp4": "Video h264/h265/av1/vp9 · text subs become mov_text · no pgs.",
    "webm": "Video vp9/av1 only · audio opus/vorbis · subs srt/vtt become webvtt.",
    "avi": "Video h264 · no subtitle streams (they are dropped from the output).",
}

VIDEO_CODECS = ("h264", "h265", "av1", "vp9")
AUDIO_CODECS = ("aac", "ac3", "eac3", "dts", "flac", "mp3", "opus", "vorbis")
SUBTITLE_CODECS = ("subrip", "vtt", "ass", "pgs")

# Profile video key -> ffprobe codec_name (comparison + expected output)
VIDEO_FFPROBE = {"h264": "h264", "h265": "hevc", "av1": "av1", "vp9": "vp9"}

# Profile key -> ffmpeg encoder
VIDEO_ENCODERS = {
    "h264": "libx264",
    "h265": "libx265",
    "av1": "libsvtav1",
    "vp9": "libvpx-vp9",
}

# Profile audio key -> ffmpeg encoder (native 'opus' is experimental;
# 'dts' has no encoder of that name — the DTS encoder is 'dca').
AUDIO_ENCODERS = {
    "aac": "aac",
    "ac3": "ac3",
    "eac3": "eac3",
    "dts": "dca",
    "flac": "flac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "vorbis": "libvorbis",
}

# Default encode parameters (option A: fixed quality + hard bitrate cap).
# vp9 cannot combine CRF with a VBV cap in this ffmpeg build, so it uses a
# capped target bitrate instead (see execute_job_plan).
VIDEO_DEFAULTS = {
    "h264": {"crf": 23, "preset": "medium"},
    "h265": {"crf": 24, "preset": "medium"},
    "av1": {"crf": 32, "preset": 8},
    "vp9": {"crf": 32, "deadline": "good", "cpu_used": 4},
}

# Which profile codecs each container can carry.
CONTAINER_VIDEO = {
    "mkv": ("h264", "h265", "av1", "vp9"),
    "mp4": ("h264", "h265", "av1", "vp9"),
    "webm": ("vp9", "av1"),
    "avi": ("h264",),
}
CONTAINER_AUDIO = {
    "mkv": ("aac", "ac3", "eac3", "dts", "flac", "mp3", "opus", "vorbis"),
    "mp4": ("aac", "ac3", "eac3", "flac", "mp3", "opus"),
    "webm": ("opus", "vorbis"),
    "avi": ("aac", "ac3", "eac3", "dts", "flac", "mp3"),
}
# Subtitle codec -> mux codec conversion when the container requires it.
# A codec absent from the map cannot be carried by the container: kept
# subtitle streams of that codec are removed by the worker.
# Container mux subtitle rules. Values:
#   - None            -> the container carries the codec natively: mux as-is
#                        (mkv accepts subrip/vtt/ass/pgs without conversion)
#   - a codec name    -> convert to that mux codec (mp4: subrip -> mov_text)
#   - missing key     -> the container cannot carry that codec (pgs in mp4)
CONTAINER_SUBTITLE = {
    "mkv": {"subrip": None, "vtt": None, "ass": None, "pgs": None, "none": None},
    "mp4": {"subrip": "mov_text", "vtt": "mov_text", "ass": "mov_text", "none": None},
    "webm": {"subrip": "webvtt", "vtt": "webvtt", "none": None},
    "avi": {"none": None},
}

# Subtitle codecs that can be transcoded into another text codec.
# Image-based subs (pgs, dvd_subtitle) cannot be converted without OCR.
# Key = source codec (normalized), value = set of convertible targets.
SUBTITLE_CONVERTIBLE = {
    "ass": {"subrip"},   # ASS -> SRT (styling is dropped)
    "vtt": {"subrip"},   # WebVTT -> SRT
}

# Subtitle track types a profile can whitelist (UI multi-select).
SUBTITLE_TYPES = ("full", "forced", "sdh", "cc")

# Words that mark a track as SDH / CC when present in its title.
_SDH_RE = re.compile(r"(?i)\b(sdh|hearing\s*impaired|for the deaf|deaf\s*subtitle)\b")
_CC_RE = re.compile(r"(?i)\b(closed\s*captions?|\bcc\b)")


def classify_subtitle_type(
    *,
    title: str | None = None,
    forced: bool = False,
    hearing_impaired: bool = False,
    captions: bool = False,
) -> str:
    """
    Classify a subtitle track type from its disposition flags + title.

    Returns one of SUBTITLE_TYPES. Flags win over title words; a track
    without any marker is 'full' (the common case).
    """
    if forced:
        return "forced"
    if hearing_impaired:
        return "sdh"
    if captions:
        return "cc"
    t = title or ""
    if "forced" in t.lower():
        return "forced"
    if _SDH_RE.search(t):
        return "sdh"
    if _CC_RE.search(t):
        return "cc"
    return "full"


def clean_subtitle_title(title: str | None) -> str:
    """
    Scrub release junk from a subtitle track title while keeping its
    semantic markers: 'English [www.newpct1.com] (SDH)' -> 'English (SDH)'.
    """
    if not title:
        return ""
    t = re.sub(r"\[[^\]]*\]", " ", title)
    t = re.sub(r"\s{2,}", " ", t).strip(" -")
    return t

# Colour transfers that identify HDR content (PQ / HLG). Tonemapped to SDR.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

# Container mux subtitle codecs are the storage equivalent of a text
# profile target (used for idempotent reprocessing + compliance).
SUBTITLE_EQUIV = {"mov_text": "subrip", "webvtt": "vtt"}


def is_valid_container(container: str | None) -> bool:
    return container in CONTAINER_EXT


def container_supports(container: str, kind: str, codec: str) -> bool:
    """Whether a profile codec is carryable in the container."""
    table = {
        "video": CONTAINER_VIDEO,
        "audio": CONTAINER_AUDIO,
        "subtitle": CONTAINER_SUBTITLE,
    }.get(kind)
    if table is None or container not in table:
        return False
    if kind == "subtitle":
        return codec in table[container]
    return codec in table[container]


def incompatible_reasons(container: str, video_codec: str, audio_codec: str,
                        subtitle_codec: str) -> list[str]:
    """Human-readable list of incompatibilities for a profile combo."""
    problems = []
    if not container_supports(container, "video", video_codec):
        problems.append(f"video {video_codec} is not supported in {container}")
    if not container_supports(container, "audio", audio_codec):
        problems.append(f"audio {audio_codec} is not supported in {container}")
    if not container_supports(container, "subtitle", subtitle_codec):
        problems.append(
            f"subtitles ({subtitle_codec}) are not supported in {container}"
        )
    return problems


# ─────────────────────────────────────────────────────────────────────
# Hardware acceleration (Immich-style install-time selection)
#
# The operator picks the backend once, at deploy time, by uncommenting
# the `extends` block in docker-compose.yml and changing ONE line:
#   service: cpu | nvenc | vaapi | qsv
# The referenced hwaccel.transcoding.yml service also injects
# THRESHERR_ACCEL into the worker container; the worker reads it and
# resolves encoders/recipes here. Codecs without a hardware encoder on
# the selected backend (e.g. av1/vp9 on nvenc) automatically fall back
# to the CPU encoder.
# ─────────────────────────────────────────────────────────────────────

HW_ACCELS = ("cpu", "nvenc", "vaapi", "qsv")

# Per-backend encoder for each profile video key. A missing codec means
# "no hardware encoder for it on this backend" -> CPU fallback.
ACCEL_VIDEO_ENCODERS = {
    "cpu": dict(VIDEO_ENCODERS),
    "nvenc": {
        "h264": "h264_nvenc",
        "h265": "hevc_nvenc",
        # av1/vp9: Turing NVENC has no AV1; VP9 NVENC is not exposed -> CPU
    },
    "vaapi": {
        "h264": "h264_vaapi",
        "h265": "hevc_vaapi",
    },
    "qsv": {
        "h264": "h264_qsv",
        "h265": "hevc_qsv",
    },
}

# Per-backend quality recipes (keys differ per encoder family: crf for
# x264/x265, cq for nvenc, global_quality for qsv, qp for vaapi).
# Starting points; the NVENC numbers get validated by the fire test.
ACCEL_VIDEO_DEFAULTS = {
    "cpu": dict(VIDEO_DEFAULTS),
    "nvenc": {
        # Calibrated 2026-09-03 (300, 720p): cq 25 visually matches x264
        # crf 23 at ~1.5x the bitrate (4.2 vs 2.7 Mbps on the sample);
        # the old cq 21 produced ~2x the bits for no visible gain.
        "h264": {"cq": 25, "preset": "p4"},
        "h265": {"cq": 26, "preset": "p4"},  # same +2 offset vs crf; validate visually on first use
    },
    "vaapi": {
        "h264": {"qp": 21},
        "h265": {"qp": 22},
    },
    "qsv": {
        "h264": {"global_quality": 21, "preset": "medium"},
        "h265": {"global_quality": 22, "preset": "medium"},
    },
}

# Encoder name -> (family, needs extra global init args before -i)
_HW_MARKERS = ("_nvenc", "_qsv", "_vaapi")


def is_valid_accel(accel: str) -> bool:
    return accel in HW_ACCELS


def encoder_family(encoder: str) -> str:
    """'cpu' | 'nvenc' | 'qsv' | 'vaapi' from an ffmpeg encoder name."""
    for marker in ("_nvenc", "_qsv", "_vaapi"):
        if marker in encoder:
            return marker[1:]
    return "cpu"


def accel_encoder(accel: str, codec: str) -> str:
    """ffmpeg encoder for (accel, codec); CPU encoder when the backend has
    no hardware encoder for that codec."""
    table = ACCEL_VIDEO_ENCODERS.get(accel) or {}
    if codec in table:
        return table[codec]
    return VIDEO_ENCODERS.get(codec, "libx264")


def accel_defaults(accel: str, codec: str) -> dict:
    """Quality recipe for (accel, codec); CPU recipe when the backend has
    no hardware encoder for that codec."""
    table = ACCEL_VIDEO_DEFAULTS.get(accel) or {}
    if codec in table:
        return dict(table[codec])
    return dict(VIDEO_DEFAULTS.get(codec, VIDEO_DEFAULTS["h264"]))
