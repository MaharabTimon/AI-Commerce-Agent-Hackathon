import json
import logging
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import config

logger = logging.getLogger(__name__)

# Facebook Messenger maximum character limit per message
MAX_MESSAGE_LENGTH = 1800

# Reusable HTTP session with connection pooling for Facebook Graph API
_messenger_session = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False
)
_messenger_session.mount("https://", HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20))


class MetaRateLimiter:
    """
    Autonomous rate-limit tracker and adaptive throttler for Meta Graph API.
    Inspects X-Page-Usage and X-App-Usage headers.
    - Usage < 75%: Normal operation with typing_on indicator enabled.
    - Usage 75% - 85%: Automatically sheds typing_on to cut API calls by 50%.
    - Usage > 85%: Automatically introduces adaptive pacing delay (1.5s - 2.5s)
      to let Meta's sliding window cool down before restrictions trigger.
    """
    def __init__(self):
        self.last_usage_pct = 0.0
        self.last_updated = 0.0
        self.window_ttl = 300.0  # 5 minutes memory
        import threading as _th
        self._lock = _th.Lock()

    def parse_headers(self, headers: dict):
        usage_val = 0.0
        for header_key in ("X-Page-Usage", "x-page-usage", "X-App-Usage", "x-app-usage"):
            raw = headers.get(header_key)
            if raw:
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    for k in ("call_count", "total_cputime", "total_time"):
                        val = float(data.get(k, 0))
                        if val > usage_val:
                            usage_val = val
                except Exception:
                    pass

        if usage_val > 0.0:
            with self._lock:
                self.last_usage_pct = usage_val
                self.last_updated = time.time()
            if self.last_usage_pct >= 75.0:
                logger.warning(f"Meta Graph API Usage Elevated: {self.last_usage_pct:.1f}%")

    def is_fresh(self) -> bool:
        return (time.time() - self.last_updated) < self.window_ttl

    def should_send_typing(self) -> bool:
        """Sheds typing indicator if Meta usage is elevated >= 75%."""
        if self.is_fresh() and self.last_usage_pct >= 75.0:
            logger.info(f"RateLimiter: High Meta usage ({self.last_usage_pct:.1f}%). Shedding typing_on to save 50% API calls.")
            return False
        return True

    def apply_pacing_delay(self):
        """Applies adaptive pacing delay if Meta usage is critical >= 85%."""
        if self.is_fresh() and self.last_usage_pct >= 85.0:
            delay = min(2.5, 1.5 + (self.last_usage_pct - 85.0) * 0.1)
            logger.warning(f"RateLimiter: Critical Meta usage ({self.last_usage_pct:.1f}%). Pacing delay {delay:.2f}s.")
            time.sleep(delay)


_rate_limiter = MetaRateLimiter()

# Outgoing send hygiene: never hammer the Graph API. Per-recipient pacing,
# a global floor between sends, dedupe of identical repeats, and a cap on
# how many chunks one customer reply may fan out into.
import hashlib as _hashlib
import threading as _threading
from collections import OrderedDict as _OrderedDict

_send_guard_lock = _threading.Lock()
_last_send_global = 0.0
_last_send_by_recip: dict = {}
_recent_sends = _OrderedDict()  # (recipient, sha) -> ts, bounded
_TYPING_MIN_INTERVAL = 8.0
_SEND_MIN_PER_RECIP = 0.8
_SEND_MIN_GLOBAL = 0.2
_DEDUPE_WINDOW = 45.0
_MAX_CHUNKS = 5
_last_typing_by_recip: dict = {}


def _wait_send_slot(recipient_id: str) -> float:
    """Sleep just long enough to respect pacing floors. Returns seconds waited."""
    global _last_send_global
    waited = 0.0
    while True:
        with _send_guard_lock:
            now = time.time()
            due = max(_last_send_by_recip.get(recipient_id, 0.0) + _SEND_MIN_PER_RECIP,
                      _last_send_global + _SEND_MIN_GLOBAL)
            wait = due - now
            if wait <= 0:
                _last_send_by_recip[recipient_id] = now
                _last_send_global = now
                if len(_last_send_by_recip) > 2000:
                    _last_send_by_recip.clear()
                return waited
        time.sleep(min(wait, 1.0))
        waited += min(wait, 1.0)
        if waited > 2.0:
            return waited


def _dedupe_key(recipient_id: str, chunk: str):
    digest = _hashlib.sha256(chunk.encode("utf-8", "ignore")).hexdigest()[:20]
    return (recipient_id, digest)


def _was_sent_recently(recipient_id: str, chunk: str) -> bool:
    """Peek only: True when this chunk was successfully sent very recently."""
    now = time.time()
    with _send_guard_lock:
        ts = _recent_sends.get(_dedupe_key(recipient_id, chunk))
        return ts is not None and (now - ts) < _DEDUPE_WINDOW


def _mark_sent(recipient_id: str, chunk: str) -> None:
    """Record a chunk only AFTER the Graph API accepted it."""
    now = time.time()
    with _send_guard_lock:
        _recent_sends[_dedupe_key(recipient_id, chunk)] = now
        _recent_sends.move_to_end(_dedupe_key(recipient_id, chunk))
        while _recent_sends and (now - next(iter(_recent_sends.values()))) > _DEDUPE_WINDOW:
            _recent_sends.popitem(last=False)
        while len(_recent_sends) > 512:
            _recent_sends.popitem(last=False)


def _split_text(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Splits long text into chunks without cutting words in half.
    Safely terminates even with leading whitespace or long continuous words.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        text = text.strip()
        if not text:
            break
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Look for the last newline or space before max_len
        split_idx = text.rfind("\n", 0, max_len)
        if split_idx <= 0:
            split_idx = text.rfind(" ", 0, max_len)
        if split_idx <= 0:
            split_idx = max_len

        chunk = text[:split_idx].strip()
        if chunk:
            chunks.append(chunk)
        text = text[split_idx:].strip()

    return chunks


def send_sender_action(recipient_id: str, action: str = "typing_on") -> bool:
    """
    Sends sender actions like 'typing_on', 'typing_off', or 'mark_seen'.
    Automatically sheds 'typing_on' if Meta API usage exceeds 75%.
    """
    if not config.PAGE_ACCESS_TOKEN:
        return False

    # Shed typing action when Meta rate limit is under pressure
    if action == "typing_on" and not _rate_limiter.should_send_typing():
        return False

    # Typing indicators are API calls too: at most one per recipient per
    # few seconds. typing_off / mark_seen always go through.
    if action == "typing_on":
        with _send_guard_lock:
            last = _last_typing_by_recip.get(recipient_id, 0.0)
            if (time.time() - last) < _TYPING_MIN_INTERVAL:
                return False
            _last_typing_by_recip[recipient_id] = time.time()
            if len(_last_typing_by_recip) > 2000:
                _last_typing_by_recip.clear()

    target = config.PAGE_ID if config.PAGE_ID else "me"
    url = f"https://graph.facebook.com/{config.MESSENGER_API_VERSION}/{target}/messages"
    payload = {
        "recipient": {"id": recipient_id},
        "sender_action": action,
    }
    params = {"access_token": config.PAGE_ACCESS_TOKEN}

    try:
        response = _messenger_session.post(url, params=params, json=payload, timeout=10)
        _rate_limiter.parse_headers(response.headers)
        return response.ok
    except requests.RequestException as e:
        logger.warning(f"Failed to send sender action '{action}': {e}")
        return False


def send_image(recipient_id: str, image_url: str) -> bool:
    """Send a native Messenger image attachment (product photo).

    Goes through the same safety gate, pacing, and dedupe as text sends so
    image replies can never hammer the Graph API either.
    """
    if not config.PAGE_ACCESS_TOKEN:
        logger.error("Missing PAGE_ACCESS_TOKEN in environment.")
        return False
    if not recipient_id or not str(recipient_id).strip():
        return False
    url = (image_url or "").strip()
    if not url.startswith("http"):
        return False
    if _was_sent_recently(recipient_id, f"img:{url}"):
        logger.info(f"Skipped duplicate image to {recipient_id}")
        return True
    _wait_send_slot(recipient_id)
    try:
        import safety as _safety
        allowed, reason = _safety.outgoing_allowed()
        if not allowed:
            logger.warning(f"Image blocked ({reason}) for {recipient_id}")
            return False
        _safety.note_outgoing()
    except Exception:
        logger.exception("safety gate error; failing closed")
        return False
    target = config.PAGE_ID if config.PAGE_ID else "me"
    api_url = f"https://graph.facebook.com/{config.MESSENGER_API_VERSION}/{target}/messages"
    payload = {
        "messaging_type": "RESPONSE",
        "recipient": {"id": recipient_id},
        "message": {"attachment": {"type": "image",
                                   "payload": {"url": url, "is_reusable": True}}},
    }
    try:
        response = _messenger_session.post(api_url, params={"access_token": config.PAGE_ACCESS_TOKEN},
                                           json=payload, timeout=15)
        _rate_limiter.parse_headers(response.headers)
        if response.ok:
            _mark_sent(recipient_id, f"img:{url}")
            logger.info(f"Image sent successfully to {recipient_id}")
            return True
        logger.error(f"Failed to send image: {response.status_code} {response.text[:200]}")
        return False
    except requests.RequestException as e:
        logger.error(f"Network error sending image to {recipient_id}: {e}")
        return False


def send_message(recipient_id: str, text: str) -> bool:
    """
    Sends a text message to a user on Facebook Messenger.
    Handles message splitting, circuit breaking, and adaptive pacing.
    """
    if not config.PAGE_ACCESS_TOKEN:
        logger.error("Missing PAGE_ACCESS_TOKEN in environment.")
        return False

    target = config.PAGE_ID if config.PAGE_ID else "me"
    url = f"https://graph.facebook.com/{config.MESSENGER_API_VERSION}/{target}/messages"
    params = {"access_token": config.PAGE_ACCESS_TOKEN}

    if not text or not text.strip():
        logger.warning(f"Refusing to send empty message to {recipient_id}")
        return False
    if not recipient_id or not str(recipient_id).strip():
        logger.warning("Refusing to send without recipient")
        return False
    _all_chunks = _split_text(text.strip())
    chunks = _all_chunks[:_MAX_CHUNKS]
    truncated = len(_all_chunks) > _MAX_CHUNKS
    if truncated:
        logger.warning(f"Reply to {recipient_id} truncated to {_MAX_CHUNKS} chunks")
    all_succeeded = True

    for chunk in chunks:
        if _was_sent_recently(recipient_id, chunk):
            logger.info(f"Skipped duplicate chunk to {recipient_id}")
            continue
        _wait_send_slot(recipient_id)
        # L4/L5 safety gate per chunk: token bucket + circuit breaker.
        try:
            import safety as _safety
            allowed, reason = _safety.outgoing_allowed()
            if not allowed:
                logger.warning(f"Outgoing blocked ({reason}) for {recipient_id}")
                return False
            _safety.note_outgoing()
        except Exception:
            logger.exception("safety gate error; failing closed")
            return False
        # Apply automatic pacing delay if Meta usage is critical (> 85%)
        _rate_limiter.apply_pacing_delay()

        payload = {
            "messaging_type": "RESPONSE",
            "recipient": {"id": recipient_id},
            "message": {"text": chunk},
        }
        try:
            response = _messenger_session.post(url, params=params, json=payload, timeout=15)
            _rate_limiter.parse_headers(response.headers)

            if response.ok:
                _mark_sent(recipient_id, chunk)
                logger.info(f"Reply sent successfully to {recipient_id}: {chunk[:50]}...")
            else:
                all_succeeded = False
                logger.error(
                    f"Failed to send message: {response.status_code} {response.text}"
                )
                # If recipient is restricted or rate-limited, immediately stop trying subsequent chunks
                break
        except requests.RequestException as e:
            all_succeeded = False
            logger.error(f"Network error sending message to {recipient_id}: {e}")
            break

    return all_succeeded and not truncated
