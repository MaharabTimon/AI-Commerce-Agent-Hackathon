"""Seller-controlled default reply language (Telegram /language).

Modes:
- auto: adapt to each customer's detected language (default)
- en:   every reply in English
- bn:   every reply in Bangla

Stored in process memory with a BOT_LANGUAGE_MODE env default; on
serverless each container converges on the seller's last choice as
traffic routes, and the env var pins the fleet-wide default.
"""
import os
import threading
import time

_lock = threading.Lock()
_mode = None
VALID = ("auto", "en", "bn")

# Serverless containers do not share memory, so the seller's choice is
# also persisted in Turso (app_settings) and read back with a short cache.
_stored_cache = {"mode": None, "at": 0.0}


def _turso_exec(sql, args=()):
    try:
        import os as _os
        import requests as _rq
        url = _os.environ.get("TURSO_DATABASE_URL", "") or ""
        tok = _os.environ.get("TURSO_AUTH_TOKEN", "") or ""
        if not url or not tok:
            return None
        base = url.replace("libsql://", "https://").rstrip("/")
        stmt_args = [{"type": "integer" if isinstance(a, int) else "text", "value": str(a)} for a in args]
        resp = _rq.post(f"{base}/v2/pipeline",
                        json={"requests": [{"type": "execute", "stmt": {"sql": sql, "args": stmt_args}},
                                           {"type": "close"}]},
                        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                        timeout=8)
        return resp.json() if resp.ok else None
    except Exception:
        return None


def _read_stored():
    now = time.time()
    if _stored_cache["mode"] is not None and now - _stored_cache["at"] < 15:
        return _stored_cache["mode"]
    _turso_exec("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    res = _turso_exec("SELECT value FROM app_settings WHERE key = 'language_mode'")
    mode = None
    try:
        val = res["results"][0]["response"]["result"]["rows"][0][0].get("value")
        if val in VALID:
            mode = val
    except Exception:
        pass
    _stored_cache.update(mode=mode, at=now)
    return mode


def _env_default() -> str:
    m = (os.environ.get("BOT_LANGUAGE_MODE") or "auto").strip().lower()
    return m if m in VALID else "auto"


def get_language_mode() -> str:
    stored = _read_stored()
    if stored:
        return stored
    with _lock:
        return _mode or _env_default()


def set_language_mode(mode: str) -> str:
    global _mode
    m = (mode or "").strip().lower()
    if m not in VALID:
        m = "auto"
    with _lock:
        _mode = m
    _stored_cache.update(mode=m, at=time.time())
    _turso_exec("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    _turso_exec("INSERT INTO app_settings (key, value) VALUES ('language_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (m,))
    return m
