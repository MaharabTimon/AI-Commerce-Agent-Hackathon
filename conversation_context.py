"""Current-product context per customer, persisted in Turso when available.

Remembers which product a customer is talking about ("can you send image?"
right after asking about a saree) across serverless containers, which
process memory cannot do. Cleared when the customer's order completes.
Falls back to process memory when no Turso database is configured.
"""
import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_mem = {}
_table_ready = False


def _turso():
    url = os.environ.get("TURSO_DATABASE_URL", "") or ""
    tok = os.environ.get("TURSO_AUTH_TOKEN", "") or ""
    if not url or not tok:
        return None
    return url.replace("libsql://", "https://").rstrip("/"), tok


def _exec(sql, args=()):
    cfg = _turso()
    if not cfg:
        return None
    base, tok = cfg
    stmt_args = []
    for a in args:
        if isinstance(a, int):
            stmt_args.append({"type": "integer", "value": str(a)})
        else:
            stmt_args.append({"type": "text", "value": str(a)})
    try:
        resp = requests.post(
            f"{base}/v2/pipeline",
            json={"requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": stmt_args}},
                {"type": "close"}]},
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            timeout=8)
        if resp.ok:
            return resp.json()
    except Exception:
        logger.debug("user_context query failed", exc_info=True)
    return None


def _ensure_table():
    global _table_ready
    if _table_ready or not _turso():
        return
    if _exec("CREATE TABLE IF NOT EXISTS user_context "
             "(user_id TEXT PRIMARY KEY, product_id INTEGER, updated_at TEXT)") is not None:
        _table_ready = True


def set_current_product(user_id, product_id) -> None:
    if not user_id or product_id is None:
        return
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return
    with _lock:
        _mem[str(user_id)] = (pid, time.time())
    _ensure_table()
    _exec("INSERT INTO user_context (user_id, product_id, updated_at) VALUES (?, ?, ?) "
          "ON CONFLICT(user_id) DO UPDATE SET product_id=excluded.product_id, updated_at=excluded.updated_at",
          (str(user_id), pid, time.strftime("%Y-%m-%d %H:%M:%S")))


def get_current_product_id(user_id):
    if not user_id:
        return None
    _ensure_table()
    res = _exec("SELECT product_id FROM user_context WHERE user_id = ?", (str(user_id),))
    try:
        rows = res["results"][0]["response"]["result"]["rows"]
        if rows and rows[0]:
            val = rows[0][0].get("value")
            return int(val) if val is not None else None
    except Exception:
        pass
    with _lock:
        row = _mem.get(str(user_id))
        if row and time.time() - row[1] < 7200:
            return row[0]
    return None


def clear(user_id) -> None:
    if not user_id:
        return
    with _lock:
        _mem.pop(str(user_id), None)
    _exec("DELETE FROM user_context WHERE user_id = ?", (str(user_id),))
