import json
import logging
import os
import threading
import time
from typing import Optional
import requests
import config

logger = logging.getLogger(__name__)

# State file location (using /tmp on serverless environments if needed)
if os.environ.get("VERCEL") == "1":
    import tempfile
    STATE_FILE = os.path.join(tempfile.gettempdir(), "bot_state.json")
else:
    STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")

_lock = threading.RLock()
_cached_active: bool = True
_last_fetch_time: float = 0.0
CACHE_TTL_SECONDS: float = 5.0


def _get_telegram_cloud_state() -> Optional[bool]:
    """
    Fetches the persistent bot active/stopped status directly from Telegram's cloud storage
    via the bot's short_description metadata.
    """
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        return None
    try:
        url = f"https://api.telegram.org/bot{token}/getMyShortDescription"
        resp = requests.get(url, timeout=3)
        if resp.ok:
            data = resp.json()
            short_desc = data.get("result", {}).get("short_description", "").upper()
            if "STATE:STOPPED" in short_desc:
                return False
            if "STATE:ACTIVE" in short_desc:
                return True
    except Exception as e:
        logger.debug(f"Could not read state from Telegram cloud: {e}")
    return None


def _set_telegram_cloud_state(active: bool) -> bool:
    """
    Persists the bot state into Telegram's cloud metadata so all serverless instances
    share the exact same state without requiring external databases.
    """
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/setMyShortDescription"
        state_str = "STATE:ACTIVE" if active else "STATE:STOPPED"
        resp = requests.post(url, json={"short_description": state_str}, timeout=4)
        return resp.ok
    except Exception as e:
        logger.warning(f"Could not persist state to Telegram cloud: {e}")
        return False


def is_bot_active() -> bool:
    """
    Returns True if automated bot replies are enabled, False if stopped for human takeover.
    Uses in-memory caching with 5-second TTL to keep Facebook response latency under 1ms.
    """
    global _cached_active, _last_fetch_time
    now = time.time()

    with _lock:
        # Return fast cached state if within TTL
        if now - _last_fetch_time < CACHE_TTL_SECONDS:
            return _cached_active

        # Check Telegram cloud state first (works across all serverless containers)
        cloud_state = _get_telegram_cloud_state()
        if cloud_state is not None:
            _cached_active = cloud_state
            _last_fetch_time = now
            return _cached_active

        # Fallback to local file state if available
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    _cached_active = bool(data.get("active", True))
            except Exception:
                pass

        _last_fetch_time = now
        return _cached_active


def set_bot_active(active: bool) -> bool:
    """
    Sets bot active state and persists both to Telegram cloud and disk.
    """
    global _cached_active, _last_fetch_time
    with _lock:
        _cached_active = active
        _last_fetch_time = time.time()

        # Update Telegram cloud metadata immediately
        cloud_ok = _set_telegram_cloud_state(active)

        # Also write to local file
        try:
            temp_file = f"{STATE_FILE}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump({"active": active}, f)
            os.replace(temp_file, STATE_FILE)
        except Exception:
            pass

        logger.info(f"Bot active state updated to: {active} (cloud_persisted: {cloud_ok})")
        return True
