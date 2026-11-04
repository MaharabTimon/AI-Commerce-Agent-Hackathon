"""Long-term customer memory and segmentation (spec 6, 7, 55, 56).

Stores only business-useful preferences: language, colors, sizes,
categories, budget, viewed and purchased products. No passwords, no
payment data, no free-form sensitive notes. Bounded and TTL-aware.
"""
import threading
import time
from collections import Counter, OrderedDict

MAX_CUSTOMERS = 2000
TTL = 90 * 86400


class CustomerMemory:
    def __init__(self):
        self._rows = OrderedDict()
        self._lock = threading.RLock()

    def _row(self, user_id):
        uid = str(user_id)
        now = time.time()
        row = self._rows.get(uid)
        if row and now - row["updated_at"] < TTL:
            self._rows.move_to_end(uid)
            row["updated_at"] = now
            return row
        if row:
            del self._rows[uid]
        if len(self._rows) >= MAX_CUSTOMERS:
            self._rows.popitem(last=False)
        row = {"user_id": uid, "name": "", "language": "", "colors": Counter(),
               "sizes": Counter(), "categories": Counter(), "budget_bdt": 0,
               "viewed": [], "purchases": [], "orders": 0, "spent_bdt": 0,
               "stage": "BROWSING", "lead_score": 0, "last_seen": now,
               "updated_at": now, "abandoned_cart_at": 0.0}
        self._rows[uid] = row
        return row

    def touch(self, user_id, language="", sentiment_stage="", score=0):
        with self._lock:
            row = self._row(user_id)
            if language:
                row["language"] = language
            if sentiment_stage:
                row["stage"] = sentiment_stage
            row["lead_score"] = max(row["lead_score"], int(score or 0))
            row["last_seen"] = time.time()
            return self._public(row)

    def note_view(self, user_id, product):
        if not product:
            return
        with self._lock:
            row = self._row(user_id)
            name = product.get("name")
            if name and name not in row["viewed"]:
                row["viewed"] = (row["viewed"] + [name])[-10:]
            if product.get("category"):
                row["categories"][product["category"]] += 1
            price = int(product.get("price_bdt") or 0)
            if price:
                row["budget_bdt"] = price if not row["budget_bdt"] else int((row["budget_bdt"] + price) / 2)

    def note_preference(self, user_id, color="", size="", budget_bdt=0):
        with self._lock:
            row = self._row(user_id)
            if color:
                row["colors"][str(color)[:30]] += 1
            if size:
                row["sizes"][str(size)[:10]] += 1
            if budget_bdt:
                row["budget_bdt"] = int(budget_bdt)

    def note_purchase(self, user_id, product_name, total_bdt):
        with self._lock:
            row = self._row(user_id)
            row["orders"] += 1
            row["spent_bdt"] += int(total_bdt or 0)
            if product_name:
                row["purchases"] = (row["purchases"] + [product_name])[-10:]
            row["stage"] = "PURCHASED"
            row["lead_score"] = max(row["lead_score"], 80)
            return self._public(row)

    def mark_abandoned_cart(self, user_id):
        with self._lock:
            self._row(user_id)["abandoned_cart_at"] = time.time()

    def _public(self, row):
        return {"user_id": row["user_id"], "name": row["name"], "language": row["language"],
                "top_colors": [c for c, _ in row["colors"].most_common(3)],
                "top_sizes": [s for s, _ in row["sizes"].most_common(3)],
                "top_categories": [c for c, _ in row["categories"].most_common(3)],
                "budget_bdt": row["budget_bdt"], "viewed": list(row["viewed"]),
                "purchases": list(row["purchases"]), "orders": row["orders"],
                "spent_bdt": row["spent_bdt"], "stage": row["stage"],
                "lead_score": row["lead_score"], "segment": self._segment(row)}

    @staticmethod
    def _segment(row):
        if row["orders"] >= 5 or row["spent_bdt"] >= 50000:
            return "VIP"
        if row["orders"] >= 2:
            return "FREQUENT_BUYER"
        if row["orders"] == 1:
            return "RETURNING_CUSTOMER"
        if row["lead_score"] >= 70:
            return "HIGH_INTENT"
        if row["budget_bdt"] and row["budget_bdt"] < 3000:
            return "PRICE_SENSITIVE"
        if time.time() - row["last_seen"] > 30 * 86400:
            return "INACTIVE"
        return "NEW_CUSTOMER"

    def _live_rows(self):
        now = time.time()
        return [r for r in self._rows.values() if now - r["updated_at"] < TTL]

    def get(self, user_id):
        with self._lock:
            row = self._rows.get(str(user_id))
            if row and time.time() - row["updated_at"] >= TTL:
                del self._rows[str(user_id)]
                return None
            return self._public(row) if row else None

    def hot_leads(self, n=10, min_score=70):
        with self._lock:
            rows = [self._public(r) for r in self._live_rows() if r["lead_score"] >= min_score]
        rows.sort(key=lambda r: r["lead_score"], reverse=True)
        return rows[:max(1, min(50, int(n)))]

    def abandoned(self, older_than_sec=3600):
        now = time.time()
        with self._lock:
            return [self._public(r) for r in self._live_rows()
                    if r["abandoned_cart_at"] and now - r["abandoned_cart_at"] >= older_than_sec
                    and r["stage"] != "PURCHASED"]

    def preference_context(self, user_id) -> str:
        profile = self.get(user_id)
        if not profile or (not profile["top_colors"] and not profile["top_categories"] and not profile["budget_bdt"]):
            return ""
        bits = []
        if profile["top_categories"]:
            bits.append("prefers " + ", ".join(profile["top_categories"]))
        if profile["top_colors"]:
            bits.append("colors " + ", ".join(profile["top_colors"]))
        if profile["budget_bdt"]:
            bits.append(f"usual budget near BDT {profile['budget_bdt']:,}")
        return "[Customer preference memory: " + "; ".join(bits) + ". Prioritize matching products.]"


_memory = CustomerMemory()


def get_memory() -> CustomerMemory:
    return _memory
