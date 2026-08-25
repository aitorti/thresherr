"""
Thresherr file naming — *arr style.

Settings -> Media Management -> Naming. Template tokens like Radarr:

    {Title} {Year} {Quality} {VideoCodec} {AudioCodec} {AudioLanguages}
    {SubtitleLanguages} {Container}

- Empty tokens leave the template untouched in place; repeated separators
  are collapsed (e.g. "[1080p h264 ES ]" -> "[1080p h264 ES]").
- Names are sanitized for the filesystem.
- Collisions get a numeric suffix " (1)" like Radarr/browsers.

Pure module: no database, no worker imports — easy to unit test.
"""

import os
import re

KNOWN_TOKENS = {
    "Title",
    "Year",
    "Quality",
    "VideoCodec",
    "AudioCodec",
    "AudioLanguages",
    "SubtitleLanguages",
    "Container",
}

# ISO 639-2 -> ISO 639-1 for compact, Radarr-like names (ES-EN instead of SPA-ENG)
_LANG_SHORT = {
    "spa": "es", "eng": "en", "fra": "fr", "deu": "de", "ita": "it",
    "por": "pt", "jpn": "ja", "chi": "zh", "zho": "zh", "rus": "ru",
    "nld": "nl", "cat": "ca", "eus": "eu", "baq": "eu", "glg": "gl",
    "pol": "pl", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi",
    "tur": "tr", "ara": "ar", "hin": "hi", "kor": "ko", "tha": "th",
    "vie": "vi", "ces": "cs", "ell": "el", "hun": "hu", "ron": "ro",
    "ukr": "uk", "heb": "he", "lat": "la",
}

_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME_LENGTH = 180


def short_language(lang: str | None) -> str | None:
    """spa -> ES, eng -> EN; unknown codes uppercased as-is; und -> None."""
    if not lang:
        return None
    lang = lang.strip().lower()
    if lang == "und":
        return None
    return _LANG_SHORT.get(lang, lang).upper()


def extract_year(title: str) -> str:
    """First 19xx/20xx year found in the original title, if any."""
    match = _YEAR_RE.search(title or "")
    return match.group(0) if match else ""


def _collapse(text: str) -> str:
    """Clean up separators left behind by empty tokens."""
    text = re.sub(r"\s+([\]\)])", r"\1", text)
    text = re.sub(r"([\[\(])\s+", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"([ _.\-])\1+", r"\1", text)
    text = re.sub(r"\s*([_\-.])\s*", r"\1", text)
    return text.strip(" _.-")


def sanitize_name(name: str) -> str:
    """Strip filesystem-invalid characters and trim for safety."""
    name = _INVALID_FS_CHARS.sub("_", name)
    name = name.strip().rstrip(".")
    if not name:
        return ""
    return name[:_MAX_NAME_LENGTH].rstrip(" _.-")


def build_output_name(
    *,
    template: str,
    title: str,
    year: str = "",
    quality: str = "",
    video_codec: str = "",
    audio_codecs: str = "",
    audio_languages: str = "",
    subtitle_languages: str = "",
    container: str = "mkv",
) -> str | None:
    """
    Render the naming template into a full file name (with extension).

    Returns None when the template is empty or produces an empty name
    (callers then keep the original file name).
    """
    if not template or not template.strip():
        return None

    values = {
        "Title": title,
        "Year": year,
        "Quality": quality,
        "VideoCodec": video_codec,
        "AudioCodec": audio_codecs,
        "AudioLanguages": audio_languages,
        "SubtitleLanguages": subtitle_languages,
        "Container": container,
    }

    out = template
    for token, value in values.items():
        out = out.replace("{" + token + "}", value or "")

    out = sanitize_name(_collapse(out))
    if not out:
        return None

    ext = (container or "mkv").lstrip(".")
    return f"{out}.{ext}"


def unique_dest_path(directory: str, file_name: str) -> str:
    """
    Return directory/file_name, appending " (1)", " (2)", ... when the
    target already exists (same behaviour as Radarr/browsers).
    """
    candidate = os.path.join(directory, file_name)
    if not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(file_name)
    counter = 1
    while True:
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        if not os.path.exists(candidate):
            return candidate
        counter += 1
