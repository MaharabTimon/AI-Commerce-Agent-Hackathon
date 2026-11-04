"""Order lifecycle, tracking, and inventory reservation (spec 22, 26-28).

States: PENDING -> CONFIRMED -> PROCESSING -> PACKED -> SHIPPED ->
OUT_FOR_DELIVERY -> DELIVERED, plus CANCELLED / RETURN_REQUESTED /
RETURNED / REFUNDED. Creating an order reserves stock first; if any
line cannot be reserved the whole order fails and nothing is taken.
Cancelling releases the reservation. Tracking never fabricates data:
unknown orders return None.
"""
import threading
import time

STATES = ["PENDING", "CONFIRMED", "PROCESSING", "PACKED", "SHIPPED",
          "OUT_FOR_DELIVERY", "DELIVERED", "CANCELLED", "RETURN_REQUESTED",
          "RETURNED", "REFUNDED"]
FLOW = ["PENDING", "CONFIRMED", "PROCESSING", "PACKED", "SHIPPED",
        "OUT_FOR_DELIVERY", "DELIVERED"]
TERMINAL = {"DELIVERED", "CANCELLED", "REFUNDED"}


class OrderError(Exception):
    pass


class OrderStore:
    def __init__(self, repo=None):
        self._orders = {}
        self._seq = 1000
        self._lock = threading.RLock()
        self._repo = repo

    def _repository(self):
        if self._repo is None:
            from database import get_product_repository
            self._repo = get_product_repository()
        return self._repo

    def _reserve(self, items):
        """Reserve stock for every line or roll all back. Returns taken list.

        Uses the repository's atomic conditional decrement so two serverless
        containers cannot both reserve the last unit, and rolls back every
        line on any failure (insufficient stock, unknown product, DB error).
        """
        repo = self._repository()
        taken = []
        try:
            for line in items:
                pid = line.get("id")
                try:
                    qty = int(line.get("qty", 1))
                except (TypeError, ValueError):
                    raise OrderError("invalid quantity")
                if qty < 1 or qty > 20:
                    raise OrderError("quantity must be between 1 and 20")
                product = repo.get_by_id(pid) if pid is not None else None
                if product is None:
                    raise OrderError(f"product {pid} is not in the catalog")
                if not repo.try_reserve_stock(pid, qty):
                    raise OrderError(f"insufficient stock for {product.get('name')}")
                taken.append((pid, qty))
        except Exception:
            for done_pid, done_qty in taken:
                repo.release_stock(done_pid, done_qty)
            raise
        return taken

    def _release(self, order):
        # Release exactly what was reserved, never the raw order lines.
        repo = self._repository()
        for pid, qty in order.get("reserved", []):
            repo.release_stock(pid, qty)

    def create(self, user_id, items, total_bdt, name="", phone="", address="", payment="cod"):
        if not items:
            raise OrderError("order has no items")
        with self._lock:
            taken = self._reserve(items)
            self._seq += 1
            order_id = f"ORD-{self._seq}"
            order = {
                "id": order_id, "user_id": str(user_id), "items": list(items),
                "total_bdt": int(total_bdt), "name": name, "phone": phone,
                "address": address, "payment": payment, "state": "CONFIRMED",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "history": [{"state": "CONFIRMED", "at": time.strftime("%H:%M:%S")}],
                "reserved": list(taken),
            }
            self._orders[order_id] = order
            while len(self._orders) > 500:  # bounded cache; oldest evicted
                self._orders.pop(next(iter(self._orders)))
            return self._public(order)

    def _public(self, order):
        out = dict(order)
        out.pop("reserved", None)
        return out

    def get(self, order_id):
        with self._lock:
            order = self._orders.get(str(order_id))
            return self._public(order) if order else None

    def find_by_phone(self, phone):
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        with self._lock:
            return [self._public(o) for o in self._orders.values()
                    if "".join(ch for ch in o["phone"] if ch.isdigit()) == digits and digits]

    # States only move along these edges; no jumps like CONFIRMED->DELIVERED.
    ALLOWED = {
        "PENDING": {"CONFIRMED", "CANCELLED"},
        "CONFIRMED": {"PROCESSING", "CANCELLED"},
        "PROCESSING": {"PACKED", "CANCELLED"},
        "PACKED": {"SHIPPED", "CANCELLED"},
        "SHIPPED": {"OUT_FOR_DELIVERY"},
        "OUT_FOR_DELIVERY": {"DELIVERED"},
        "DELIVERED": {"RETURN_REQUESTED"},
        "RETURN_REQUESTED": {"RETURNED", "REFUNDED"},
        "RETURNED": {"REFUNDED"},
        "CANCELLED": set(),
        "REFUNDED": set(),
    }

    def transition(self, order_id, new_state):
        if new_state not in STATES:
            raise OrderError(f"unknown state {new_state}")
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None:
                raise OrderError("order not found")
            if new_state not in self.ALLOWED.get(order["state"], set()):
                raise OrderError(f"cannot move {order['state']} to {new_state}")
            if new_state == "CANCELLED":
                self._release(order)
            order["state"] = new_state
            order["history"].append({"state": new_state, "at": time.strftime("%H:%M:%S")})
            return self._public(order)

    def request_return(self, order_id, reason=""):
        with self._lock:
            order = self._orders.get(str(order_id))
            if order is None:
                raise OrderError("order not found")
            if order["state"] != "DELIVERED":
                raise OrderError(f"order in state {order['state']} cannot be returned yet")
            try:
                from business_rules import get_rules
                window = get_rules().return_window_days
            except Exception:
                window = 7
            try:
                created = time.mktime(time.strptime(order["created_at"], "%Y-%m-%d %H:%M:%S"))
                if time.time() - created > window * 86400:
                    raise OrderError(f"return window of {window} days has passed")
            except ValueError:
                pass
            order["state"] = "RETURN_REQUESTED"
            order["return_reason"] = reason
            order["history"].append({"state": "RETURN_REQUESTED", "at": time.strftime("%H:%M:%S")})
            return self._public(order)

    def tracking_text(self, order_id) -> str:
        order = self.get(order_id)
        if order is None:
            return ""
        lines = [f"Order {order['id']} - BDT {order['total_bdt']:,}"]
        current = order["state"]
        for state in FLOW:
            mark = "done" if current in FLOW and FLOW.index(state) < FLOW.index(current) else (
                "now" if state == current else "pending")
            lines.append(f"{state}: {mark}")
        if current not in FLOW:
            lines.append(f"Status: {current}")
        return "\n".join(lines)

    def list_recent(self, n=20):
        with self._lock:
            rows = list(self._orders.values())
        return [self._public(o) for o in rows[-max(1, min(100, int(n))):]]


_store = OrderStore()


def get_order_store() -> OrderStore:
    return _store
