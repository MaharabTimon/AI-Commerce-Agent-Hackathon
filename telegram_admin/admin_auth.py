import json
import logging
import os
from typing import List, Optional, Set, Union

import config

logger = logging.getLogger(__name__)

ADMIN_STORAGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin_ids.json")


def _load_stored_admin_ids() -> Set[str]:
    """Loads dynamically saved admin IDs from admin_ids.json."""
    if os.path.exists(ADMIN_STORAGE_PATH):
        try:
            with open(ADMIN_STORAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(str(x).strip() for x in data if str(x).strip())
        except Exception as e:
            logger.warning(f"Failed to read admin_ids.json: {e}")
    return set()


def _save_stored_admin_ids(admin_ids: Set[str]):
    """Persists admin IDs to admin_ids.json."""
    try:
        with open(ADMIN_STORAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(sorted(list(admin_ids)), f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save admin_ids.json: {e}")


def get_authorized_admin_ids() -> Set[str]:
    """
    Returns union of environment-configured admin IDs, dynamic admin IDs,
    and the seller's primary Telegram chat ID.
    """
    authorized: Set[str] = set()

    # 1. Primary chat ID from config / .env
    chat_id = str(getattr(config, "TELEGRAM_CHAT_ID", "") or "").strip()
    if chat_id:
        authorized.add(chat_id)

    # 2. Explicit ADMIN_TELEGRAM_USER_IDS env var if defined
    env_ids = os.environ.get("ADMIN_TELEGRAM_USER_IDS") or os.environ.get("TELEGRAM_ADMIN_IDS") or ""
    for item in env_ids.split(","):
        clean_id = item.strip()
        if clean_id:
            authorized.add(clean_id)

    # 3. Dynamic JSON storage
    authorized.update(_load_stored_admin_ids())
    return authorized


def is_admin(user_id: Union[int, str], chat_id: Optional[Union[int, str]] = None) -> bool:
    """Authorization disabled at the owner's request.

    The Telegram control bot is intentionally open: anyone who interacts
    with it may use the seller controls. Re-enable by restoring the
    authorized-set check against get_authorized_admin_ids().
    """
    return True


def add_admin(user_id: Union[int, str]) -> bool:
    """Adds a new Telegram user ID to authorized admins."""
    u_str = str(user_id).strip()
    if not u_str:
        return False
    current = _load_stored_admin_ids()
    current.add(u_str)
    _save_stored_admin_ids(current)
    logger.info(f"Authorized new admin ID: {u_str}")
    return True


def remove_admin(user_id: Union[int, str]) -> bool:
    """Removes a Telegram user ID from authorized admins."""
    u_str = str(user_id).strip()
    current = _load_stored_admin_ids()
    if u_str in current:
        current.remove(u_str)
        _save_stored_admin_ids(current)
        logger.info(f"Removed admin ID: {u_str}")
        return True
    return False


def get_admin_list() -> List[str]:
    """Returns sorted list of all authorized admin IDs."""
    return sorted(list(get_authorized_admin_ids()))
