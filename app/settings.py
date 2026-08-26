"""
Settings helpers (key/value table) — single source of truth.

The settings table is used by every module (app, worker, tasks, health...).
This module centralizes the read/write helpers so the same logic is not
reimplemented in each one.

Usage:
    import settings
    settings.get_setting(db, "log_level", "info")
    settings.set_setting(db, "ui_language", "es")
    settings.get_int(db, "backup_interval_days", 7)
"""

import models


def get_setting(db, key: str, default: str | None = None) -> str | None:
    """Read a setting value; `default` when unset/empty."""
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if row and row.value is not None:
        return row.value
    return default


def set_setting(db, key: str, value: str) -> None:
    """Upsert a setting value (caller commits)."""
    row = db.query(models.Setting).filter(models.Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))


def get_int(db, key: str, default: int = 0) -> int:
    """Read a setting as int; `default` when unset or not a number."""
    try:
        return int(get_setting(db, key, default))
    except (TypeError, ValueError):
        return default
