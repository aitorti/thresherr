"""
Automatic language detection cascade (*arr philosophy: resolve 'und'
streams automatically, the human only decides as a last resort).

Audio cascade (run in decreasing scope after a scan):
    1. mkvinfo/mkvmerge -J   (Matroska native properties + track names)
    2. mediainfo             (generic container reader, any container)

Text-subtitle cascade:
    1. fastText (lid.176) on a sample of the subtitle text
       (image subtitles — PGS/VobSub — have no text; they stay 'und')

When a language is found for a .mkv file, mkvpropedit writes the tag
into the container: 'und' is cured at the source (ffprobe sees it next
time) and the worker output inherits it. Non-Matroska files are solved
at the summary level (and the worker tags the output it produces).

Whisper is NOT part of the automatic cascade (CPU cost): it is exposed
as an on-demand button in the inspect modal (see main.py).
"""

import json
import os
import re
import subprocess
import urllib.request

# Track-name keywords per language (mkvinfo/mediainfo "title" fallback)
_TRACK_NAME_HINTS = {
    "spa": ["castellano", "espanol", "español", "spanish", "castilian", "latino", "latam", "es"],
    "eng": ["english", "ingles", "inglés", "vo", "original", "en"],
    "fra": ["french", "francais", "français", "vff", "vfq", "fr"],
    "ita": ["italian", "italiano", "it"],
    "deu": ["german", "aleman", "alemán", "deutsch", "de"],
    "por": ["portuguese", "portugues", "portugués", "pt"],
    "jpn": ["japanese", "japones", "japonés", "ja"],
    "chi": ["chinese", "chino", "mandarin", "cantonese", "zh"],
    "rus": ["russian", "ruso", "ru"],
    "nld": ["dutch", "holandes", "holandés", "nl"],
}

# Common 2-letter codes -> ISO-639-2 (resumen format), plus the
# bibliographic forms MKVToolNix writes (fre/ger/dut...) -> terminological
_LANG_MAP = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita",
    "pt": "por", "ja": "jpn", "zh": "chi", "ru": "rus", "nl": "nld",
    "fre": "fra", "ger": "deu", "dut": "nld",
}

_UND_CODES = {"und", "unk", "unknown", "undefined", "", "none", "null", "-"}

# fastText lid.176 model (Facebook, ~1 MB, cached in the data volume)
_LID_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
_LID_MODEL_DIR = os.environ.get("THRESHERR_MODEL_DIR", "/data/models")
_LID_MODEL_PATH = os.path.join(_LID_MODEL_DIR, "lid.176.bin")

# Idiomas que el detector puede proponer (los que los perfiles suelen usar)
_DETECTABLE = {"spa", "eng", "fra", "ita", "deu", "por", "jpn", "chi", "rus", "nld"}


def _run(cmd, timeout=30):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except Exception:
        return -1, "", ""


def _normalize_lang(lang: str | None) -> str | None:
    """'es'/'Spanish'/'spa' -> 'spa'; None for undetermined codes."""
    if not lang:
        return None
    value = lang.strip().lower()
    if value in _UND_CODES:
        return None
    if value in _LANG_MAP:
        return _LANG_MAP[value]
    if value in _DETECTABLE:
        return value
    # full names / 3-letter unknown -> hint matching later
    return value if len(value) <= 3 else None


def _lang_from_name(name: str | None) -> str | None:
    """Language hinted by a track name ('Castellano', 'VO English'...)."""
    if not name:
        return None
    n = name.lower()
    for iso, hints in _TRACK_NAME_HINTS.items():
        if any(h in n for h in hints):
            return iso
    return None


def _apply_to_summary(summary: str | None, detected: list[str]) -> str | None:
    """
    Replace the 'und' entries of a summary with the detected languages,
    in order. Extra detections are appended when there were no 'und' left.
    """
    if not detected:
        return summary
    parts = [p.strip() for p in (summary or "").split(",") if p.strip()]
    out = []
    di = 0
    for p in parts:
        if p == "und" and di < len(detected):
            out.append(detected[di])
            di += 1
        else:
            out.append(p)
    for extra in detected[di:]:
        if extra not in out:
            out.append(extra)
    return ", ".join(out)


def has_und_in_summary(media) -> bool:
    """True when the stored summary still contains 'und' (audio or subs)."""
    for value in (media.audio_languages, media.subtitle_languages):
        if value and any(p.strip() == "und" for p in value.split(",")):
            return True
    return False


# -------------------------------------------------
# Pass 1: mkvinfo / mkvmerge (Matroska only)
# -------------------------------------------------

def detect_audio_with_mkvinfo(path: str) -> list[dict]:
    """
    Audio tracks without a native language tag, resolved from the Matroska
    properties or the track name. Returns [{track_id, language, source}].
    """
    if not path.lower().endswith(".mkv"):
        return []
    rc, out, _ = _run(["mkvmerge", "-J", path])
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    results = []
    for track in data.get("tracks", []) or []:
        if track.get("type") != "audio":
            continue
        props = track.get("properties", {}) or {}
        native = _normalize_lang(props.get("language"))
        if native:
            continue  # already tagged
        detected = _lang_from_name(props.get("track_name"))
        if detected:
            results.append(
                {"track": track.get("id"), "language": detected, "source": "mkvinfo"}
            )
    return results


def tag_language_in_mkv(path: str, track_id, language: str) -> bool:
    """Write the language tag into a Matroska container (no re-encode)."""
    rc, _, _ = _run(
        ["mkvpropedit", path, "--edit", f"track:@{track_id}", "--set", f"language={language}"],
        timeout=60,
    )
    return rc == 0


# -------------------------------------------------
# Pass 2: mediainfo (any container)
# -------------------------------------------------

def detect_audio_with_mediainfo(path: str) -> list[dict]:
    """Audio tracks resolved from MediaInfo (Language or Title)."""
    rc, out, _ = _run(["mediainfo", "--Output=JSON", path])
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    results = []
    for track in (data.get("media", {}) or {}).get("track", []) or []:
        if track.get("@type") != "Audio":
            continue
        detected = _normalize_lang(track.get("Language"))
        if not detected:
            detected = _lang_from_name(track.get("Title") or track.get("Language_More"))
        if detected and detected in _DETECTABLE:
            results.append(
                {
                    "track": track.get("StreamOrder"),
                    "language": detected,
                    "source": "mediainfo",
                }
            )
    return results


# -------------------------------------------------
# Subtitles: fastText (text subs only)
# -------------------------------------------------

def _lid_model_path() -> str:
    os.makedirs(_LID_MODEL_DIR, exist_ok=True)
    if not os.path.exists(_LID_MODEL_PATH):
        tmp = _LID_MODEL_PATH + ".tmp"
        urllib.request.urlretrieve(_LID_MODEL_URL, tmp)
        os.replace(tmp, _LID_MODEL_PATH)
    return _LID_MODEL_PATH


def _extract_subtitle_text(path: str, track_idx: int = 0) -> str:
    """First text-subtitle track as plain text ('' for image subs)."""
    rc, out, _ = _run(
        ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:s:{track_idx}", "-f", "srt", "-"],
        timeout=60,
    )
    if rc != 0 or not out.strip():
        return ""
    text = re.sub(r"^\s*\d+\s*$", "", out, flags=re.M)
    text = re.sub(
        r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}",
        "",
        text,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\\[^}]*\}", "", text)
    return text.strip()


def detect_subtitle_language_fasttext(path: str) -> list[dict]:
    """Language of the first TEXT subtitle track (fastText lid.176)."""
    text = _extract_subtitle_text(path)
    if len(text) < 40:
        return []
    try:
        import fasttext
    except ImportError:
        return []
    try:
        model = fasttext.load_model(_lid_model_path())
        labels, _ = model.predict(text[:2000].replace("\n", " "))
        lang = labels[0].replace("__label__", "")
        lang = _normalize_lang(lang)
        if lang and lang in _DETECTABLE:
            return [{"track": 0, "language": lang, "source": "fasttext"}]
    except Exception:
        pass
    return []


# -------------------------------------------------
# Per-file resolution used by the cascade passes
# -------------------------------------------------

def resolve_with_mkvinfo(media, db) -> bool:
    """Pass 1: resolve audio 'und' via Matroska info + tag the container."""
    detected = detect_audio_with_mkvinfo(media.full_path)
    if not detected:
        return False
    langs = [d["language"] for d in detected]
    media.audio_languages = _apply_to_summary(media.audio_languages, langs)
    if media.full_path.lower().endswith(".mkv"):
        for d in detected:
            tag_language_in_mkv(media.full_path, d["track"], d["language"])
    db.commit()
    return True


def resolve_with_mediainfo(media, db) -> bool:
    """Pass 2: audio via MediaInfo + text subs via fastText."""
    resolved = False
    detected_audio = detect_audio_with_mediainfo(media.full_path)
    if detected_audio:
        langs = [d["language"] for d in detected_audio]
        media.audio_languages = _apply_to_summary(media.audio_languages, langs)
        resolved = True

    if media.subtitle_languages and any(
        p.strip() == "und" for p in media.subtitle_languages.split(",")
    ):
        detected_subs = detect_subtitle_language_fasttext(media.full_path)
        if detected_subs:
            langs = [d["language"] for d in detected_subs]
            media.subtitle_languages = _apply_to_summary(media.subtitle_languages, langs)
            resolved = True

    if resolved:
        db.commit()
    return resolved
