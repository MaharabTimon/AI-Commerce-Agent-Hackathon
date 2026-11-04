"""Demand + funnel analytics (overview Phase 6). In-memory, thread-safe.

Demand rows record what customers asked for but could not buy (out of
stock or not in catalog) so the owner gets a restock signal.
"""
import threading
import time
from collections import Counter, OrderedDict

MAX_DEMAND_KEYS = 500


class Analytics:
    def __init__(self):
        self._lock = threading.Lock()
        self._demand = Counter()
        self._funnel = Counter()
        self._orders_value_bdt = 0
        self._day = time.strftime("%Y-%m-%d")

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._funnel.clear()
            self._demand.clear()
            self._orders_value_bdt = 0

    def record_demand(self, query: str):
        q = (query or "").strip()[:120]
        if not q:
            return
        with self._lock:
            self._roll_day()
            self._demand[q.lower()] += 1
            if len(self._demand) > MAX_DEMAND_KEYS:
                self._demand = Counter(dict(self._demand.most_common(MAX_DEMAND_KEYS // 2)))

    def record_event(self, name: str, value_bdt: int = 0):
        with self._lock:
            self._roll_day()
            self._funnel[name] += 1
            if name == "order_confirmed":
                try:
                    self._orders_value_bdt += max(0, int(value_bdt or 0))
                except (TypeError, ValueError):
                    pass

    def top_demand(self, n=5):
        with self._lock:
            return self._demand.most_common(max(1, min(20, int(n))))

    def daily_summary(self) -> str:
        with self._lock:
            self._roll_day()
            f = dict(self._funnel)
            value = self._orders_value_bdt
            top = self._demand.most_common(5)
        lines = [
            f"Daily summary ({self._day})",
            f"Greetings: {f.get('greeting', 0)} | Product inquiries: {f.get('product_inquiry', 0)}",
            f"Orders started: {f.get('order_started', 0)} | Orders confirmed: {f.get('order_confirmed', 0)}",
            f"Sales value: BDT {value:,}",
        ]
        if top:
            lines.append("Top unmet demand: " + ", ".join(f"{q} x{c}" for q, c in top))
        return "\n".join(lines)


_analytics = Analytics()


def get_analytics() -> Analytics:
    return _analytics
