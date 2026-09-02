"""
Connect / notifications — *arr style (System -> Connect).

Three notification kinds (Telegram / Webhook / Script), each subscribed to a
configurable set of events:

    ScanCompleted    a scan found new file(s)              (tasks.run_scan)
    JobCompleted     a media file finished OK               (worker)
    JobFailed        a media file failed                    (worker)
    UndBlocked       enqueue blocked on und streams         (app)
    BackupCompleted  a backup was created                   (app / worker)
    HealthIssue      the health issue set changed           (worker, hourly)

Design rules (mirroring the *arr family):
  - English is the only notification language (logs too).
  - Dispatching is synchronous with short timeouts. Events only fire on
    state transitions (never inside the worker loop), so the blocking is
    negligible at home scale.
  - A failing connection NEVER raises into the caller: it is logged and the
    other connections still get their event.
  - Secrets (bot tokens) are never logged.

Usage:
    import connect
    connect.fire_event(db, "JobCompleted", {"fileName": "...", ...})
"""

import html
import json
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone

import models
import settings
import i18n
from logging_setup import get_logger

logger = get_logger("connect")

INSTANCE_NAME = "Thresherr"

# All events a connection can subscribe to (labels live in the i18n files)
EVENTS = (
    "ScanCompleted",
    "JobCompleted",
    "JobFailed",
    "UndBlocked",
    "BackupCompleted",
    "HealthIssue",
)

KINDS = ("telegram", "webhook", "script")

TELEGRAM_API = "https://api.telegram.org"
HTTP_TIMEOUT = 8
SCRIPT_TIMEOUT = 20


# -------------------------------------------------
# CRUD (UI + API share this module)
# -------------------------------------------------

def list_connections(db) -> list[models.Connection]:
    return db.query(models.Connection).order_by(models.Connection.id).all()


def get_connection(db, conn_id: int) -> models.Connection | None:
    return (
        db.query(models.Connection)
        .filter(models.Connection.id == conn_id)
        .first()
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def save_connection(db, *, conn_id=None, name, kind, enabled, events, config) -> models.Connection:
    """Create or update a connection and return it (caller commits)."""
    conn = None
    if conn_id is not None:
        conn = get_connection(db, conn_id)
    if conn is None:
        conn = models.Connection(
            name=name,
            kind=kind,
            enabled=enabled,
            events=json.dumps(events),
            config=json.dumps(config),
            created_at=_utcnow_naive(),
        )
        db.add(conn)
    else:
        conn.name = name
        conn.kind = kind
        conn.enabled = enabled
        conn.events = json.dumps(events)
        conn.config = json.dumps(config)
        conn.updated_at = _utcnow_naive()
    return conn


def delete_connection(db, conn_id: int) -> bool:
    conn = get_connection(db, conn_id)
    if conn is None:
        return False
    db.delete(conn)
    return True


def serialize(conn: models.Connection) -> dict:
    """Plain dict for templates and the API."""
    try:
        events = json.loads(conn.events or "[]")
    except Exception:
        events = []
    try:
        config = json.loads(conn.config or "{}")
    except Exception:
        config = {}
    if not isinstance(events, list):
        events = []
    if not isinstance(config, dict):
        config = {}
    return {
        "id": conn.id,
        "name": conn.name,
        "kind": conn.kind,
        "enabled": bool(conn.enabled),
        "events": events,
        "config": config,
        "created_at": conn.created_at.isoformat() if conn.created_at else None,
        "updated_at": conn.updated_at.isoformat() if conn.updated_at else None,
    }


# -------------------------------------------------
# Payloads
# -------------------------------------------------

def payload(event: str, data: dict) -> dict:
    """Common JSON payload for Webhook/Script (mirrors the *arr shape)."""
    return {
        "eventType": event,
        "instanceName": INSTANCE_NAME,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": data,
    }


def _fmt_bytes(size: int | None) -> str:
    if not size:
        return "0 B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


# -------------------------------------------------
# Human text (Telegram messages, English, *arr style)
# -------------------------------------------------

def _health_text(issue: dict) -> str:
    # Issue keys already carry the 'health.' prefix (e.g. 'health.und_blocked').
    key = issue.get("key") or "health.unknown"
    args = issue.get("args") or {}
    text = i18n.get_text("en", key, **args)
    if text == key:
        text = key
    return text


def _telegram_text(event: str, data: dict) -> str:
    """Compose the HTML message for an event (dynamic text is escaped)."""
    if event == "ScanCompleted":
        return f"🌾 <b>Scan completed</b> — {html.escape(data.get('result') or '')}"
    if event == "JobCompleted":
        lines = [
            "✅ <b>" + html.escape(data.get("fileName") or "?") + "</b>",
            "📚 " + html.escape(data.get("library") or "?"),
        ]
        orig = _fmt_bytes(data.get("sizeOriginal"))
        final = _fmt_bytes(data.get("sizeFinal"))
        lines.append(f"💾 {orig} → {final}")
        if data.get("savingsBytes"):
            lines.append(
                f"🎉 Saved {_fmt_bytes(data['savingsBytes'])} "
                f"({data.get('savingsPct', 0):.1f}%)"
            )
        return "\n".join(lines)
    if event == "JobFailed":
        err = html.escape(str(data.get("error") or "?")[:500])
        return (
            "❌ <b>" + html.escape(data.get("fileName") or "?") + "</b>\n"
            "📚 " + html.escape(data.get("library") or "?") + "\n"
            "⚠️ " + err
        )
    if event == "UndBlocked":
        return (
            "🚧 <b>" + html.escape(data.get("fileName") or "?") + "</b>\n"
            "Unknown language (und) — assign languages to unblock"
        )
    if event == "BackupCompleted":
        return "🗄️ <b>Backup created</b> — " + html.escape(data.get("file") or "?")
    if event == "HealthIssue":
        issues = data.get("issues") or []
        lines = [f"⚠️ <b>Health: {len(issues)} issue(s)</b>"]
        for issue in issues:
            lines.append("• " + html.escape(_health_text(issue)))
        return "\n".join(lines)
    # Test
    return "🧪 <b>Test</b> — Thresherr connected!"


# -------------------------------------------------
# Dispatch (one connection kind)
# -------------------------------------------------

def _send_telegram(config: dict, text: str) -> tuple[bool, str]:
    token = (config.get("bot_token") or "").strip()
    chat_id = (config.get("chat_id") or "").strip()
    if not token or not chat_id:
        return False, "Telegram: bot_token and chat_id are required"
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    body = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            result = json.loads(resp.read() or b"{}")
        if result.get("ok"):
            return True, "Telegram: message sent"
        desc = result.get("description") or "unknown error"
        return False, f"Telegram: API error: {desc}"
    except Exception as exc:
        # Never log the token: the URL contains it.
        return False, f"Telegram: request failed: {exc}"


def _send_webhook(config: dict, event: str, data: dict) -> tuple[bool, str]:
    url = (config.get("url") or "").strip()
    if not url:
        return False, "Webhook: url is required"
    body = json.dumps(payload(event, data)).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
        if 200 <= status < 300:
            return True, f"Webhook: delivered ({status})"
        return False, f"Webhook: HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"Webhook: HTTP {exc.code}"
    except Exception as exc:
        return False, f"Webhook: request failed: {exc}"


def _run_script(config: dict, event: str, data: dict) -> tuple[bool, str]:
    path = (config.get("script_path") or "").strip()
    if not path:
        return False, "Script: script_path is required"
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False, f"Script: not found or not executable: {path}"
    env = dict(os.environ)
    env["THRESHERR_EVENT"] = event
    try:
        proc = subprocess.run(
            [path, event],
            input=json.dumps(payload(event, data)),
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Script: timed out after {SCRIPT_TIMEOUT}s"
    except Exception as exc:
        return False, f"Script: could not run: {exc}"
    if proc.returncode == 0:
        return True, "Script: exited 0"
    tail = (proc.stderr or proc.stdout or "").strip()[-300:]
    return False, f"Script: exit {proc.returncode}: {tail}"


def dispatch(conn: models.Connection, event: str, data: dict) -> tuple[bool, str]:
    """Send one event through one connection. Never raises."""
    try:
        config = json.loads(conn.config or "{}")
    except Exception:
        config = {}
    if not isinstance(config, dict):
        config = {}

    if event == "Test":
        # Test notifications use the same text as the real events.
        if conn.kind == "telegram":
            return _send_telegram(config, _telegram_text("Test", data))
        if conn.kind == "webhook":
            return _send_webhook(config, "Test", data)
        return _run_script(config, "Test", data)

    if conn.kind == "telegram":
        return _send_telegram(config, _telegram_text(event, data))
    if conn.kind == "webhook":
        return _send_webhook(config, event, data)
    return _run_script(config, event, data)


# -------------------------------------------------
# Public entry points (used by the app and the worker)
# -------------------------------------------------

def fire_event(db, event: str, data: dict) -> dict:
    """
    Deliver an event to every enabled connection subscribed to it.
    Failures are logged per connection; this never raises.
    Returns {"notified": n, "failed": n}.
    """
    notified = 0
    failed = 0
    for conn in list_connections(db):
        try:
            subscribed = json.loads(conn.events or "[]")
        except Exception:
            subscribed = []
        if not conn.enabled or not isinstance(subscribed, list) or event not in subscribed:
            continue
        ok, message = dispatch(conn, event, data)
        if ok:
            notified += 1
            logger.info(
                "Connect '%s' (%s) delivered event %s", conn.name, conn.kind, event
            )
        else:
            failed += 1
            logger.warning(
                "Connect '%s' (%s) FAILED event %s: %s",
                conn.name,
                conn.kind,
                event,
                message,
            )
    if notified or failed:
        logger.info(
            "Event %s: %s connection(s) notified, %s failed", event, notified, failed
        )
    return {"notified": notified, "failed": failed}


def test_connection(db, conn_id: int) -> dict:
    """Send a Test event through one connection. Returns {ok, message}."""
    conn = get_connection(db, conn_id)
    if conn is None:
        return {"ok": False, "message": "connection not found"}
    ok, message = dispatch(conn, "Test", {})
    if ok:
        logger.info("Connect '%s' (%s): test OK", conn.name, conn.kind)
    else:
        logger.warning("Connect '%s' (%s): test FAILED: %s", conn.name, conn.kind, message)
    return {"ok": ok, "message": message}


def notify_health_if_changed(db, issues: list[dict]) -> bool:
    """
    Fire HealthIssue only when the set of issues changed since the last
    notification (stored in settings as 'connect_health_sig'). Returns True
    when an event was fired.
    """
    signature = json.dumps(
        sorted(f"{i.get('source', '?')}.{i.get('key', '?')}" for i in issues),
        sort_keys=True,
    )
    previous = settings.get_setting(db, "connect_health_sig", "")
    if signature == previous:
        return False
    settings.set_setting(db, "connect_health_sig", signature)
    db.commit()
    if issues:
        fire_event(db, "HealthIssue", {"issues": issues})
        return True
    return False
