import threading
import time
from typing import Any, Dict, Optional


class AdminState:
    IDLE = "IDLE"
    WAITING_SEARCH = "WAITING_SEARCH"
    WAITING_ADD_PRODUCT = "WAITING_ADD_PRODUCT"
    WAITING_PRICE_EDIT = "WAITING_PRICE_EDIT"
    WAITING_STOCK_EDIT = "WAITING_STOCK_EDIT"
    WAITING_COLOR_EDIT = "WAITING_COLOR_EDIT"
    WAITING_SPECS_EDIT = "WAITING_SPECS_EDIT"
    WAITING_IMPORT_FILE = "WAITING_IMPORT_FILE"
    WAITING_IMPORT_CURRENCY = "WAITING_IMPORT_CURRENCY"
    CONFIRM_DELETE = "CONFIRM_DELETE"
    CONFIRM_REPLACE = "CONFIRM_REPLACE"


class AdminSession:
    def __init__(self, user_id: str):
        self.user_id: str = user_id
        self.state: str = AdminState.IDLE
        self.data: Dict[str, Any] = {}
        self.panel_message_id: Optional[int] = None
        self.prompt_message_id: Optional[int] = None
        self.last_active: float = time.time()

    def set_state(self, state: str, **kwargs):
        self.state = state
        self.data.update(kwargs)
        self.last_active = time.time()

    def clear(self):
        self.state = AdminState.IDLE
        self.data.clear()
        self.prompt_message_id = None
        self.last_active = time.time()

    def is_expired(self, ttl_seconds: float = 900.0) -> bool:
        """Sessions expire after 15 minutes of inactivity."""
        return (time.time() - self.last_active) > ttl_seconds


_admin_sessions: Dict[str, AdminSession] = {}
_session_lock = threading.Lock()


def get_admin_session(user_id: Any) -> AdminSession:
    """Retrieves or creates an active session for an admin user."""
    uid_str = str(user_id).strip()
    with _session_lock:
        for _uid in [k for k, s in list(_admin_sessions.items()) if s.is_expired()]:
            _admin_sessions.pop(_uid, None)
        while len(_admin_sessions) >= 500:
            _admin_sessions.pop(next(iter(_admin_sessions)))
        session = _admin_sessions.get(uid_str)
        if session is None or session.is_expired():
            session = AdminSession(uid_str)
            _admin_sessions[uid_str] = session
        else:
            session.last_active = time.time()
        return session


def clear_admin_session(user_id: Any):
    """Resets an admin's conversation state back to IDLE."""
    uid_str = str(user_id).strip()
    with _session_lock:
        _admin_sessions.pop(uid_str, None)
