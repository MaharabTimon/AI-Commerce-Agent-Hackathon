"""Multi-product cart (overview Phase 5). Thread-safe, bounded, TTL-aware."""
import threading
import time
from collections import OrderedDict

MAX_QTY = 20
MAX_ITEMS = 20
TTL = 86400
MAX_USERS = 2000


def _price_bdt(product) -> int:
    try:
        return max(0, int(product.get("price_bdt") or 0))
    except (TypeError, ValueError):
        return 0


class CartStore:
    def __init__(self):
        self._carts = OrderedDict()
        self._lock = threading.RLock()

    def _cart(self, user_id):
        now = time.time()
        uid = str(user_id)
        if uid in self._carts:
            cart, ts = self._carts[uid]
            if now - ts < TTL:
                self._carts.move_to_end(uid)
                self._carts[uid] = (cart, now)
                return cart
            del self._carts[uid]
        if len(self._carts) >= MAX_USERS:
            self._carts.popitem(last=False)
        cart = {}
        self._carts[uid] = (cart, now)
        return cart

    def add(self, user_id, product, qty=1):
        if not product or not product.get("name"):
            return None
        qty = max(1, min(MAX_QTY, int(qty or 1)))
        with self._lock:
            cart = self._cart(user_id)
            pid = product.get("id") or product.get("name")
            if pid not in cart and len(cart) >= MAX_ITEMS:
                return None
            cur = cart.get(pid, {"product": product, "qty": 0})
            cap = MAX_QTY
            try:
                stock = int(product.get("stock", 0) or 0)
                if stock > 0:
                    cap = min(cap, stock)
            except (TypeError, ValueError):
                pass
            cur["qty"] = max(1, min(cap, cur["qty"] + qty))
            cur["product"] = product
            cart[pid] = cur
            return self.view(user_id)

    def set_qty(self, user_id, product_id, qty):
        with self._lock:
            cart = self._cart(user_id)
            if qty <= 0:
                cart.pop(product_id, None)
            elif product_id in cart:
                cart[product_id]["qty"] = max(1, min(MAX_QTY, int(qty)))
            return self.view(user_id)

    def remove(self, user_id, product_id):
        with self._lock:
            self._cart(user_id).pop(product_id, None)
            return self.view(user_id)

    def clear(self, user_id):
        with self._lock:
            self._carts.pop(str(user_id), None)

    def view(self, user_id):
        with self._lock:
            cart = self._cart(user_id)
            items = []
            total = 0
            for pid, row in cart.items():
                p = row["product"]
                line = _price_bdt(p) * row["qty"]
                total += line
                items.append({"id": pid, "name": p.get("name"), "qty": row["qty"],
                              "unit_price_bdt": _price_bdt(p), "line_total_bdt": line})
            return {"items": items, "total_bdt": total, "count": sum(i["qty"] for i in items)}


_store = CartStore()


def get_cart_store() -> CartStore:
    return _store
