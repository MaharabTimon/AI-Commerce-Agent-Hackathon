"""Append-only audit log for sensitive actions (spec 75, 85).

Every tool call and seller action records WHO did WHAT, WHEN, and the
RESULT. Bounded in-memory ring; secrets and message bodies are never
stored, only short summaries.
"""
import threading
import time
from collections import deque

MAX_ENTRIES = 2000


class AuditLog:
    def __init__(self):
        self._rows = deque(maxlen=MAX_ENTRIES)
        self._lock = threading.Lock()

    def record(self, actor: str, action: str, detail: str = "", result: str = "ok",
               request_id: str = ""):
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "actor": str(actor)[:64],
            "action": str(action)[:80],
            "detail": str(detail)[:300],
            "result": str(result)[:40],
            "request_id": str(request_id)[:40],
        }
        with self._lock:
            self._rows.append(row)
        return row

    def recent(self, n: int = 20, action: str = ""):
        with self._lock:
            rows = list(self._rows)
        if action:
            rows = [r for r in rows if action in r["action"]]
        return rows[-max(1, min(200, int(n))):]

    def format_recent(self, n: int = 10) -> str:
        rows = self.recent(n)
        if not rows:
            return "No audit entries yet."
        return "\n".join(
            f"{r['ts']} | {r['actor']} | {r['action']} | {r['detail']} | {r['result']}"
            for r in rows)


_log = AuditLog()


def get_audit_log() -> AuditLog:
    return _log
