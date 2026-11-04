"""Five-layer page safety net (overview Phase 2).

L1 echo/self guard, L2 anti-loop, L3 human pacing jitter,
L4 token-bucket rate limiter, L5 circuit breaker (>25 sends/60s trips).

All state is in-memory and thread-safe. On serverless each container has
its own budget, so the breaker is a per-container fuse: still stops a
runaway loop in the hot container and alerts the owner.
"""
import hashlib
import os
import logging
import random
import threading
import time
from collections import OrderedDict, deque

logger = logging.getLogger(__name__)

BREAKER_LIMIT = 25
BREAKER_WINDOW = 60.0
BUCKET_CAPACITY = 20.0
BUCKET_REFILL_PER_SEC = 0.5


class CircuitBreaker:
    def __init__(self, limit=BREAKER_LIMIT, window=BREAKER_WINDOW):
        self.limit = limit
        self.window = window
        self._events = deque()
        self._tripped = False
        self._lock = threading.Lock()
        self.on_trip = None  # callable(reason: str)

    def record_send(self) -> bool:
        """Record one outgoing send. Returns False when tripped."""
        with self._lock:
            if self._tripped:
                return False
            now = time.time()
            while self._events and now - self._events[0] > self.window:
                self._events.popleft()
            self._events.append(now)
            if len(self._events) > self.limit:
                self._tripped = True
                logger.error("Circuit breaker TRIPPED: %s sends in %.0fs", len(self._events), self.window)
                cb = self.on_trip
            else:
                cb = None
        if cb:
            try:
                cb(f"{self.limit}+ sends in {self.window:.0f}s")
            except Exception:
                logger.exception("circuit-breaker alert callback failed")
        return not self._tripped

    def is_tripped(self) -> bool:
        with self._lock:
            if self._tripped and self._events and (time.time() - self._events[-1] > self.window):
                # Half-open: the burst window has passed, allow sends again.
                self._tripped = False
                self._events.clear()
                logger.info("Circuit breaker auto-reset after quiet window")
            return self._tripped

    def reset(self) -> None:
        with self._lock:
            self._tripped = False
            self._events.clear()
        logger.info("Circuit breaker manually reset")


class TokenBucket:
    def __init__(self, capacity=BUCKET_CAPACITY, refill=BUCKET_REFILL_PER_SEC):
        self.capacity = float(capacity)
        self.refill = float(refill)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class AntiLoopGuard:
    """Mutes a sender who repeats near-identical messages rapidly."""

    def __init__(self, max_repeats=4, window=60.0, max_senders=2000):
        self.max_repeats = max_repeats
        self.window = window
        self.max_senders = max_senders
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(sender_id, text):
        h = hashlib.sha256((text or "").strip().lower().encode("utf-8", "ignore")).hexdigest()[:16]
        return f"{sender_id}:{h}"

    def is_looping(self, sender_id, text) -> bool:
        if not sender_id or not text:
            return False
        key = self._key(sender_id, text)
        now = time.time()
        with self._lock:
            hits = [t for t in self._seen.get(key, []) if now - t <= self.window]
            hits.append(now)
            if key not in self._seen and len(self._seen) >= self.max_senders:
                self._seen.popitem(last=False)
            self._seen[key] = hits
            self._seen.move_to_end(key)
            return len(hits) > self.max_repeats


breaker = CircuitBreaker()
bucket = TokenBucket()
anti_loop = AntiLoopGuard()


def human_delay() -> float:
    """Natural reading+typing pause (L3). Returns seconds slept.

    Serverless functions have a hard execution ceiling, so pacing there is
    a short beat instead of a full human pause.
    """
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        delay = random.uniform(0.15, 0.35)
    else:
        delay = 1.2 + random.uniform(0.3, 0.8)
    time.sleep(delay)
    return delay


def outgoing_allowed() -> tuple:
    """(allowed, reason) combining L4 bucket and L5 breaker."""
    if breaker.is_tripped():
        return False, "circuit_breaker_tripped"
    if not bucket.allow():
        return False, "rate_limited"
    return True, ""


def note_outgoing() -> bool:
    return breaker.record_send()
