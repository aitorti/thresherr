"""
Local artwork for the UI (posters).

Priority: the artwork that ALREADY lives next to the media. Radarr, Sonarr,
Kodi and Jellyfin all drop poster files inside the movie/show folder, so most
installations have it already. Nothing is downloaded here: a remote provider
(TMDB) is a later, optional step and only matters for the folders that have no
local art at all.

The image is copied ONCE into the data volume, cropped to the grid ratio, so
the UI never reads the (slow, network) library on every page load.
"""

import os

from logging_setup import get_logger

logger = get_logger("artwork")

# Portrait candidates, in priority order (Radarr writes folder.jpg, Kodi and
# Jellyfin may write poster.jpg). Landscape art (fanart/backdrop/landscape) is
# deliberately NOT a poster: cropping a 16:9 image to 2:3 looks awful.
POSTER_CANDIDATES = (
    "poster.jpg", "poster.png",
    "folder.jpg", "folder.png",
    "cover.jpg", "cover.png",
    "movie.jpg", "movie.png",
    "show.jpg", "show.png",
    "default.jpg", "default.png",
    "thumb.jpg", "thumb.png",
)

POSTER_SIZE = (400, 600)          # 2:3, the shape of the UI grid
POSTER_DIR = os.environ.get("THRESHERR_POSTER_DIR", "/data/posters")


def poster_cache_path(media_id: int) -> str:
    return os.path.join(POSTER_DIR, "%s.jpg" % media_id)


def cached_ids() -> set:
    """Ids that already have a cached poster (one listdir, cheap)."""
    ids = set()
    try:
        names = os.listdir(POSTER_DIR)
    except OSError:
        return ids
    for name in names:
        if not name.endswith(".jpg"):
            continue
        stem = name.rsplit(".", 1)[0]
        # Keep integers as integers: templates compare them with media.id
        ids.add(int(stem) if stem.isdigit() else stem)
    return ids


def find_local_poster(media_path: str, library_root: str | None = None) -> str | None:
    """First local poster image for a media file, or None.

    Looks inside the file's own folder first, then at the library root (TV
    libraries point at the show folder, the episodes live in season subfolders).
    """
    folder = os.path.dirname(media_path)
    directories = [folder]
    if library_root and os.path.normpath(library_root) != os.path.normpath(folder):
        directories.append(library_root)

    stem = os.path.splitext(os.path.basename(media_path))[0].lower()
    for directory in directories:
        try:
            present = {name.lower(): name for name in os.listdir(directory)}
        except OSError:
            continue
        for candidate in POSTER_CANDIDATES:
            if candidate in present:
                return os.path.join(directory, present[candidate])
        # Kodi style: "<file name>.jpg" next to the media itself
        for lower, real in present.items():
            if lower.endswith((".jpg", ".png")) and lower.startswith(stem):
                return os.path.join(directory, real)
    return None


def ensure_poster(media, library=None, force: bool = False) -> bool:
    """Cache the local poster of `media`, resized. True when one is available."""
    target = poster_cache_path(media.id)
    if os.path.exists(target) and not force:
        return True

    source = find_local_poster(
        media.full_path, getattr(library, "media_path", None)
    )
    if source is None:
        return False

    try:
        from PIL import Image, ImageOps

        os.makedirs(POSTER_DIR, exist_ok=True)
        with Image.open(source) as image:
            thumb = ImageOps.fit(image.convert("RGB"), POSTER_SIZE)
            thumb.save(target, "JPEG", quality=85)
        return True
    except Exception as exc:
        logger.warning("Could not build poster for %s: %s", media.full_path, exc)
        return False


def prune_cache(valid_ids) -> int:
    """Delete cached posters whose media row is gone. Returns how many."""
    valid = {str(i) for i in valid_ids}
    removed = 0
    try:
        names = os.listdir(POSTER_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".jpg"):
            continue
        if name.rsplit(".", 1)[0] in valid:
            continue
        try:
            os.remove(os.path.join(POSTER_DIR, name))
            removed += 1
        except OSError:
            pass
    return removed
