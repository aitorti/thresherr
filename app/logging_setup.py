"""
Thresherr logging — *arr style.

Mirrors the logging conventions of the Sonarr/Radarr family so that
Thresherr feels at home next to them:

- Rolling log files (1 MB x 5 backups) split by level:
      thresherr.txt         -> configured level (default INFO)
      thresherr.debug.txt   -> always DEBUG+
      thresherr.trace.txt   -> always TRACE+
- Line format: 2026-08-25 18:30:00.123|INFO|Thresherr.Worker|message
- Every record also lands in the `logs` SQLite table (System -> Logs UI)
- Sensitive values (apikey, password, token, ...) are masked in every sink
- Timestamps are rendered in Europe/Madrid (Aitor's timezone)

Usage:
    from logging_setup import setup_logging, get_logger
    setup_logging()
    logger = get_logger("worker")
    logger.info("processing media_file id=%s", job.id)
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

# Python has no TRACE; the *arr family does. Add it below DEBUG.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

# Display names match the *arr family (WARN instead of WARNING, FATAL instead of CRITICAL)
LEVEL_NAMES = {
    "TRACE": "TRACE",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARN",
    "ERROR": "ERROR",
    "CRITICAL": "FATAL",
}

# Log level names accepted by the settings UI (same set as the *arr family)
CONFIGURABLE_LEVELS = {
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": TRACE,
}
DEFAULT_LEVEL_NAME = "info"

LOG_DIR = os.environ.get("THRESHERR_LOG_DIR", "/data/logs")
LOG_FILE_MAIN = "thresherr.txt"
LOG_FILE_DEBUG = "thresherr.debug.txt"
LOG_FILE_TRACE = "thresherr.trace.txt"
MAX_BYTES = 1024 * 1024  # 1 MB per file, like the *arr family
BACKUP_COUNT = 5

ROOT_LOGGER_NAME = "thresherr"

# -------------------------------------------------
# Secret masking
# -------------------------------------------------

_SECRET_PATTERNS = [
    # key=value / key: value forms (apikey, api_key, password, passwd, token, secret, authorization)
    (
        re.compile(
            r"(apikey|api_key|api-key|password|passwd|token|secret|authorization)"
            r"(\s*[:=]\s*)[^\s,;\"']+",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
    # URL with embedded credentials: https://user:pass@host -> https://***@host
    (
        re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE),
        r"\1***@",
    ),
]


def mask_secrets(text: str) -> str:
    """
    Redact sensitive values in a log message.

    Applied to every sink (files, console, database) so secrets never
    leave the app, matching the *arr family behaviour.
    """
    masked = text
    for pattern, replacement in _SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def _display_logger_name(name: str) -> str:
    """thresherr.worker -> Thresherr.worker (matching the *arr 'Sonarr.X' look)."""
    if name == ROOT_LOGGER_NAME:
        return "Thresherr"
    return name.replace(ROOT_LOGGER_NAME + ".", "Thresherr.", 1)


# -------------------------------------------------
# *arr-style formatter
# -------------------------------------------------

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("Europe/Madrid")
except Exception:  # pragma: no cover - exotic platform fallback
    _LOCAL_TZ = None


def _format_timestamp(epoch: float) -> str:
    """2026-08-25 18:30:00.123 in Europe/Madrid (fallback: local time)."""
    if _LOCAL_TZ is not None:
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(_LOCAL_TZ)
    else:
        dt = datetime.fromtimestamp(epoch)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{int(dt.microsecond / 1000):03d}"


class ArrFormatter(logging.Formatter):
    """
    Format: 2026-08-25 18:30:00.123|INFO|Thresherr.Worker|message
    Exceptions are appended below the line, like the *arr file logs.
    """

    def format(self, record: logging.LogRecord) -> str:
        timestamp = _format_timestamp(record.created)
        level = LEVEL_NAMES.get(record.levelname, record.levelname)
        logger_name = _display_logger_name(record.name)

        message = mask_secrets(record.getMessage())

        line = f"{timestamp}|{level}|{logger_name}|{message}"

        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            line += "\n" + mask_secrets(exc_text)

        return line


# -------------------------------------------------
# Database handler (System -> Logs UI)
# -------------------------------------------------

_DB_MAX_ROWS = 1000
_DB_PURGE_EVERY = 200


class DatabaseLogHandler(logging.Handler):
    """
    Persists log records into the `logs` SQLite table.

    Import of SQLAlchemy is deferred so this module stays importable in
    environments without the app dependencies (e.g. unit tests). If the
    database is unavailable, records are dropped silently: logging must
    never break the application.
    """

    def __init__(self, max_rows: int = _DB_MAX_ROWS, purge_every: int = _DB_PURGE_EVERY):
        # DEBUG+ only: TRACE records (e.g. every HTTP request) stay in the
        # trace file, they would only add noise to the System -> Logs table.
        super().__init__(level=logging.DEBUG)
        self.max_rows = max_rows
        self.purge_every = purge_every
        self._since_purge = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from database import engine
            from sqlalchemy import text

            message = mask_secrets(record.getMessage())
            exception = None
            if record.exc_info:
                exception = mask_secrets(self.formatException(record.exc_info))

            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO logs (time, level, logger, message, exception) "
                        "VALUES (:time, :level, :logger, :message, :exception)"
                    ),
                    {
                        "time": datetime.now(timezone.utc).replace(tzinfo=None),
                        "level": LEVEL_NAMES.get(record.levelname, record.levelname),
                        "logger": _display_logger_name(record.name),
                        "message": message,
                        "exception": exception,
                    },
                )

                self._since_purge += 1
                if self._since_purge >= self.purge_every:
                    conn.execute(
                        text(
                            "DELETE FROM logs WHERE id NOT IN "
                            "(SELECT id FROM logs ORDER BY id DESC LIMIT :max_rows)"
                        ),
                        {"max_rows": self.max_rows},
                    )
                    self._since_purge = 0
        except Exception:
            # Logging must never crash the app; drop the record silently.
            pass


# -------------------------------------------------
# Level persistence (Settings -> General -> Log level)
# -------------------------------------------------

def _load_level_from_db() -> str:
    """Read the persisted log level name; 'info' when unavailable."""
    try:
        from database import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM settings WHERE key = 'log_level'")
            ).fetchone()
            if row and row[0] in CONFIGURABLE_LEVELS:
                return row[0]
    except Exception:
        pass
    return DEFAULT_LEVEL_NAME


def _save_level_to_db(level_name: str) -> None:
    """Persist the log level so it survives restarts."""
    try:
        from database import engine
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings (key, value) VALUES ('log_level', :value) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                ),
                {"value": level_name},
            )
    except Exception:
        pass


# -------------------------------------------------
# Public API
# -------------------------------------------------

_handlers: list[logging.Handler] = []


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the thresherr root (e.g. 'thresherr.worker')."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def setup_logging() -> None:
    """
    Configure the thresherr root logger with the *arr-style sinks.

    Safe to call more than once (idempotent): existing handlers are
    removed first, so app and worker can each call it at startup.
    """
    global _handlers

    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    _handlers = []

    os.makedirs(LOG_DIR, exist_ok=True)

    # 1. Console (docker logs) — everything
    console = logging.StreamHandler()
    console.setLevel(TRACE)
    console.setFormatter(ArrFormatter())
    _handlers.append(console)

    # 2. Main file — configured level (default INFO)
    main_file = RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE_MAIN),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    main_file.setLevel(CONFIGURABLE_LEVELS[_load_level_from_db()])
    main_file.setFormatter(ArrFormatter())
    _handlers.append(main_file)

    # 3. Debug file — always DEBUG+
    debug_file = RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE_DEBUG),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    debug_file.setLevel(logging.DEBUG)
    debug_file.setFormatter(ArrFormatter())
    _handlers.append(debug_file)

    # 4. Trace file — always TRACE+
    trace_file = RotatingFileHandler(
        os.path.join(LOG_DIR, LOG_FILE_TRACE),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    trace_file.setLevel(TRACE)
    trace_file.setFormatter(ArrFormatter())
    _handlers.append(trace_file)

    # 5. Database (System -> Logs UI)
    db_handler = DatabaseLogHandler()
    db_handler.setFormatter(ArrFormatter())
    _handlers.append(db_handler)

    # Root logger must let everything through; handlers filter by level.
    root.setLevel(TRACE)
    for handler in _handlers:
        root.addHandler(handler)

    # Keep the module logger quiet on noisy libraries (uvicorn access log, etc.)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_log_level() -> str:
    """Current main-file level name ('info' | 'debug' | 'trace')."""
    for handler in _handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename.endswith(LOG_FILE_MAIN):
            for name, value in CONFIGURABLE_LEVELS.items():
                if handler.level == value:
                    return name
    return DEFAULT_LEVEL_NAME


def set_log_level(level_name: str) -> str:
    """
    Change the main log file level at runtime (no restart needed),
    persisting the choice in the database. Returns the applied name.
    """
    level_name = level_name.lower()
    if level_name not in CONFIGURABLE_LEVELS:
        raise ValueError(f"invalid log level: {level_name}")

    for handler in _handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename.endswith(LOG_FILE_MAIN):
            handler.setLevel(CONFIGURABLE_LEVELS[level_name])

    _save_level_to_db(level_name)
    return level_name
