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

# Default encode parameters (option A: fixed quality + hard bitrate cap).
# vp9 cannot combine CRF with a VBV cap in this ffmpeg build, so it uses a
# capped target bitrate instead (see execute_job_plan).
VIDEO_DEFAULTS = {
    "h264": {"crf": 20, "preset": "medium"},
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
CONTAINER_SUBTITLE = {
    "mkv": {"subrip": None, "vtt": None, "ass": None, "pgs": None, "none": None},
    "mp4": {"subrip": "mov_text", "vtt": "mov_text", "ass": "mov_text", "none": None},
    "webm": {"subrip": "webvtt", "vtt": "webvtt", "none": None},
    "avi": {"none": None},
}

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
